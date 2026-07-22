from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field, TypeAdapter, model_validator

from vsm.activation.reorientation import ReorientationTurn
from vsm.errors import InvariantViolation, ModelMismatch, ReconciliationRequired
from vsm.ids import new_id
from vsm.interface.models import InterfaceAction
from vsm.kernel.models import Identifier, NonBlank, StrictModel, WorkItem
from vsm.pilot.models import (
    DeviceIdentity,
    EventDeltaSummary,
    InterfacePilotUsage,
    InterfaceResumePack,
    InterfaceTurn,
    ModelCandidate,
    PilotMode,
    StructuredInterfaceResponse,
)


class PilotHostReceiptError(InvariantViolation):
    def __init__(self, receipt: "PilotHostReceipt") -> None:
        self.receipt = receipt
        code = receipt.error.code if receipt.error is not None else "UnknownFailure"
        message = (
            receipt.error.message
            if receipt.error is not None
            else "PilotHost returned a failed receipt without an error"
        )
        super().__init__(f"{code}: {message}")


class PilotHostTransportUnknown(ReconciliationRequired):
    def __init__(self, receipt: "PilotHostReceipt") -> None:
        self.receipt = receipt
        super().__init__(
            f"PilotHost receipt {receipt.receipt_id} is transport_unknown; "
            "artifact and Effect reconciliation is required"
        )


class PilotHostModelMismatch(ModelMismatch):
    def __init__(self, receipt: "PilotHostReceipt") -> None:
        self.receipt = receipt
        super().__init__(
            f"RequestedActualModelMismatch: requested={receipt.requested_model}, "
            f"actual={receipt.actual_model}"
        )


class PilotHostUnreachable(ReconciliationRequired):
    def __init__(self, receipt_id: str) -> None:
        self.receipt_id = receipt_id
        super().__init__(
            f"PilotHost POST outcome and receipt {receipt_id} are unreachable"
        )


class ReceiptError(StrictModel):
    code: NonBlank
    message: NonBlank


class PilotHostReceipt(StrictModel):
    receipt_id: NonBlank
    endpoint: Literal[
        "/v1/interface-turn",
        "/v1/reorientation-turn",
        "/v1/work-executions",
    ]
    idempotency_key: NonBlank
    request_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    status: Literal["in_progress", "succeeded", "failed", "transport_unknown"]
    candidate_key: NonBlank
    requested_model: NonBlank | None
    actual_model: NonBlank | None
    provider_session_id: NonBlank | None
    usage: dict[str, Any] | None
    result: dict[str, Any] | None
    error: ReceiptError | None
    created_at: NonBlank
    updated_at: NonBlank

    @model_validator(mode="after")
    def terminal_fields_are_consistent(self) -> "PilotHostReceipt":
        if self.status == "succeeded":
            if (
                self.actual_model is None
                or self.provider_session_id is None
                or self.usage is None
                or self.result is None
                or self.error is not None
            ):
                raise ValueError("succeeded PilotHost receipt is incomplete")
        elif self.status in ("failed", "transport_unknown"):
            if self.result is not None or self.error is None:
                raise ValueError("failed PilotHost receipt fields are inconsistent")
        return self


class ResumeReferencePack(StrictModel):
    node_memory_refs: tuple[str, ...]
    unfinished_work_item_ids: tuple[str, ...]
    open_commitment_ids: tuple[str, ...]
    active_decision_ids: tuple[str, ...]


class ArtifactReference(StrictModel):
    artifact_id: Identifier
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    media_type: NonBlank


class WorkExecutionProfile(StrictModel):
    cwd: NonBlank
    sandbox: Literal["read-only", "workspace-write"]
    max_input_tokens: Annotated[int, Field(gt=0)]
    max_output_tokens: Annotated[int, Field(gt=0)]
    max_total_tokens: Annotated[int, Field(gt=0)]
    timeout_seconds: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def total_covers_parts(self) -> "WorkExecutionProfile":
        if self.max_total_tokens < self.max_input_tokens + self.max_output_tokens:
            raise ValueError("work max_total_tokens must cover input and output")
        return self


class WorkExecutionResult(StrictModel):
    summary: NonBlank
    acceptance_results: tuple[dict[str, Any], ...]
    artifact_refs: tuple[str, ...]
    event_notes: tuple[str, ...]
    completed: bool


class WorkExecutionOutcome(StrictModel):
    receipt: PilotHostReceipt
    result: WorkExecutionResult


class ProductionPilotHostClient:
    """Exact production receipt contract. It never calls the local verification host."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        identity: DeviceIdentity,
        interface_candidate: ModelCandidate,
        coding_candidate: ModelCandidate,
        permission_mode: PilotMode,
        interface_max_budget_usd: float,
        interface_timeout_seconds: float,
        work_profile: WorkExecutionProfile,
        transport_timeout_seconds: float,
        preflight_expectation: dict[str, object] | None = None,
    ) -> None:
        if (
            not base_url
            or not bearer_token
            or interface_max_budget_usd <= 0
            or interface_timeout_seconds <= 0
            or transport_timeout_seconds <= 0
        ):
            raise InvariantViolation("production PilotHost fields must be explicit")
        self.identity = identity
        self.interface_candidate = interface_candidate
        self.coding_candidate = coding_candidate
        self.permission_mode = permission_mode
        self.interface_max_budget_usd = interface_max_budget_usd
        self.interface_timeout_seconds = interface_timeout_seconds
        self.work_profile = work_profile
        self.preflight_expectation = (
            None if preflight_expectation is None else dict(preflight_expectation)
        )
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "X-Nanihold-Pilot-Host-Id": identity.pilot_host_id,
                "X-Nanihold-Device-Id": identity.device_id,
                "X-Nanihold-Device-Certificate-Sha256": (
                    identity.certificate_sha256
                ),
            },
            timeout=transport_timeout_seconds,
        )
        response = self._client.get("/health")
        self._raise_http(response)
        health = response.json()
        self._validate_health(health)

    def close(self) -> None:
        self._client.close()

    def respond(
        self, *, owner_text: str, context: InterfaceTurn
    ) -> StructuredInterfaceResponse:
        receipt_id = new_id("receipt")
        payload = self._claude_base(
            receipt_id=receipt_id,
            idempotency_key=f"interface:{receipt_id}",
            event_delta=context.event_delta,
            root_session_id=context.provider_session_id,
            resume_pack=context.resume_pack,
        )
        payload["owner_text"] = owner_text
        receipt = self._post_receipt("/v1/interface-turn", payload, receipt_id)
        return self._interface_response(receipt, root_session_id=context.provider_session_id)

    def respond_reorientation(
        self,
        context: ReorientationTurn,
    ) -> StructuredInterfaceResponse:
        receipt_id = new_id("receipt")
        payload = self._claude_base(
            receipt_id=receipt_id,
            idempotency_key=f"reorientation:{receipt_id}",
            event_delta=context.event_delta,
            root_session_id=context.provider_session_id,
            resume_pack=None,
        )
        payload.update(
            {
                "objective": context.objective,
                "session_index_ref": context.session_index_ref,
                "open_commitment_refs": list(context.open_commitment_refs),
                "current_state_ref": context.current_state_ref,
                "history_result": context.history_result.model_dump(mode="json"),
                "assessment_contract": context.assessment_contract.model_dump(
                    mode="json"
                ),
                "audited_history_event_ids": list(context.audited_history_event_ids),
                "session_index_event_ids": list(context.session_index_event_ids),
                "session_index_summary": context.session_index_summary.model_dump(
                    mode="json"
                ),
                "assessment_contract_included": context.assessment_contract_included,
            }
        )
        receipt = self._post_receipt(
            "/v1/reorientation-turn", payload, receipt_id
        )
        return self._interface_response(
            receipt, root_session_id=context.provider_session_id
        )

    def execute_work(
        self,
        *,
        execution_id: str,
        work_item: WorkItem,
        candidate: ModelCandidate,
        event_delta: EventDeltaSummary,
        artifact_refs: tuple[ArtifactReference, ...],
        idempotency_key: str,
        agent_name: str,
    ) -> WorkExecutionOutcome:
        if candidate.key != self.coding_candidate.key:
            raise InvariantViolation(
                "selected coding candidate differs from production PilotHost"
            )
        receipt_id = new_id("receipt")
        handoff = {
            "work_item_id": work_item.work_item_id,
            "title": work_item.title,
            "objective": work_item.description,
        }
        handoff["agent_name"] = agent_name
        payload = {
            "receipt_id": receipt_id,
            "idempotency_key": idempotency_key,
            "device_identity": self.identity.model_dump(mode="json"),
            "candidate": self._candidate(candidate),
            "execution_id": execution_id,
            "work_item": handoff,
            "unmet_acceptance": list(work_item.acceptance_criteria),
            "event_delta": event_delta.model_dump(mode="json"),
            "artifact_refs": [
                item.model_dump(mode="json") for item in artifact_refs
            ],
            "cwd": self.work_profile.cwd,
            "sandbox": self.work_profile.sandbox,
            "token_budget": {
                "max_input_tokens": self.work_profile.max_input_tokens,
                "max_output_tokens": self.work_profile.max_output_tokens,
                "max_total_tokens": self.work_profile.max_total_tokens,
            },
            "timeout_seconds": self.work_profile.timeout_seconds,
        }
        receipt = self._post_receipt(
            "/v1/work-executions", payload, receipt_id
        )
        result = WorkExecutionResult.model_validate(receipt.result)
        return WorkExecutionOutcome(receipt=receipt, result=result)

    def validate_work_candidate(self, candidate: ModelCandidate) -> None:
        if candidate.key != self.coding_candidate.key:
            raise InvariantViolation(
                "selected coding candidate differs from production PilotHost"
            )

    def _claude_base(
        self,
        *,
        receipt_id: str,
        idempotency_key: str,
        event_delta: EventDeltaSummary,
        root_session_id: str | None,
        resume_pack: InterfaceResumePack | None,
    ) -> dict[str, object]:
        return {
            "receipt_id": receipt_id,
            "idempotency_key": idempotency_key,
            "device_identity": self.identity.model_dump(mode="json"),
            "candidate": self._candidate(self.interface_candidate),
            "permission_mode": self.permission_mode.value,
            "max_budget_usd": self.interface_max_budget_usd,
            "timeout_seconds": self.interface_timeout_seconds,
            "root_session_id": root_session_id,
            "fork_session": root_session_id is not None,
            "event_delta": event_delta.model_dump(mode="json"),
            "resume_pack": (
                None if resume_pack is None else self._resume_refs(resume_pack)
            ),
        }

    @staticmethod
    def _candidate(candidate: ModelCandidate) -> dict[str, object]:
        return candidate.model_dump(mode="json", exclude_computed_fields=True)

    @staticmethod
    def _resume_refs(pack: InterfaceResumePack) -> dict[str, object]:
        def refs(items: tuple[dict[str, object], ...], field: str) -> tuple[str, ...]:
            values = []
            for item in items:
                value = item.get(field)
                if not isinstance(value, str) or not value:
                    raise InvariantViolation(
                        f"Interface resume pack is missing {field}"
                    )
                values.append(value)
            return tuple(values)

        return ResumeReferencePack(
            node_memory_refs=refs(pack.node_memory, "memory_id"),
            unfinished_work_item_ids=refs(
                pack.unfinished_work_items, "work_item_id"
            ),
            open_commitment_ids=refs(pack.open_commitments, "commitment_id"),
            active_decision_ids=refs(pack.active_decisions, "decision_id"),
        ).model_dump(mode="json")

    def _post_receipt(
        self, endpoint: str, payload: dict[str, object], receipt_id: str
    ) -> PilotHostReceipt:
        try:
            response = self._client.post(endpoint, json=payload)
        except httpx.TransportError:
            receipt = self._get_receipt(receipt_id)
        else:
            self._raise_http(response)
            receipt = PilotHostReceipt.model_validate(response.json())
        self._validate_receipt(endpoint, payload, receipt)
        if receipt.status == "transport_unknown":
            raise PilotHostTransportUnknown(receipt)
        if receipt.status == "failed":
            if (
                receipt.error is not None
                and receipt.error.code == "RequestedActualModelMismatch"
            ):
                raise PilotHostModelMismatch(receipt)
            raise PilotHostReceiptError(receipt)
        if receipt.status != "succeeded":
            raise InvariantViolation(
                "PilotHost returned a non-terminal receipt from synchronous RPC"
            )
        return receipt

    def _get_receipt(self, receipt_id: str) -> PilotHostReceipt:
        try:
            response = self._client.get(f"/v1/receipts/{receipt_id}")
        except httpx.TransportError as exc:
            raise PilotHostUnreachable(receipt_id) from exc
        self._raise_http(response)
        return PilotHostReceipt.model_validate(response.json())

    def _interface_response(
        self, receipt: PilotHostReceipt, *, root_session_id: str | None
    ) -> StructuredInterfaceResponse:
        if receipt.usage is None or receipt.result is None:
            raise InvariantViolation("succeeded Interface receipt lacks result or usage")
        result = receipt.result
        display_text = result.get("display_text")
        raw_actions = result.get("actions")
        if not isinstance(display_text, str) or not isinstance(raw_actions, list):
            raise InvariantViolation("PilotHost Interface result violates contract")
        action_adapter = TypeAdapter(list[InterfaceAction])
        actions = tuple(action_adapter.validate_python(raw_actions))
        usage = receipt.usage
        provider_session_id = receipt.provider_session_id
        if provider_session_id is None:
            raise InvariantViolation("PilotHost did not establish a root session")
        return StructuredInterfaceResponse(
            display_text=display_text,
            actions=actions,
            provider_session_id=provider_session_id,
            pilot_usage=InterfacePilotUsage(
                candidate_key=self.interface_candidate.key,
                actual_provider=self.interface_candidate.provider,
                actual_model_snapshot=receipt.actual_model,
                input_tokens=usage["input_tokens"],
                cache_creation_input_tokens=usage["cache_creation_input_tokens"],
                cache_read_input_tokens=usage["cache_read_input_tokens"],
                output_tokens=usage["output_tokens"],
                cost_usd=usage["cost_usd"],
                duration_ms=usage["duration_ms"],
                classifier_triggered=usage["classifier_triggered"],
                model_substitution=usage["model_substitution"],
                full_history_resent=False,
                polling_call=False,
                false_complete=False,
                reedited_tokens=0,
            ),
        )

    def _validate_health(self, health: object) -> None:
        if not isinstance(health, dict) or health.get("status") != "ready":
            raise InvariantViolation("production PilotHost is not ready")
        if health.get("identity") != self.identity.model_dump(mode="json"):
            raise InvariantViolation("production PilotHost identity mismatch")
        candidates = health.get("candidates")
        if not isinstance(candidates, dict):
            raise InvariantViolation("production PilotHost health lacks candidates")
        expected = {
            "interface": self.interface_candidate,
            "coding_s1": self.coding_candidate,
        }
        for role, candidate in expected.items():
            actual = candidates.get(role)
            if not isinstance(actual, dict) or (
                actual.get("candidate_key") != candidate.key
                or actual.get("selection") != candidate.selection
                or actual.get("model_snapshot") != candidate.model_snapshot
                or actual.get("effort") != candidate.effort
            ):
                raise InvariantViolation(
                    f"production PilotHost {role} candidate mismatch"
                )
        if health.get("permission_mode") != self.permission_mode.value:
            raise InvariantViolation("production PilotHost permission mode mismatch")
        if health.get("receipt_reconciliation") is not True:
            raise InvariantViolation("production PilotHost lacks receipt reconciliation")
        if self.preflight_expectation is not None:
            if health.get("preflight") != self.preflight_expectation:
                raise InvariantViolation(
                    "production PilotHost preflight configuration mismatch"
                )

    def _validate_receipt(
        self,
        endpoint: str,
        payload: dict[str, object],
        receipt: PilotHostReceipt,
    ) -> None:
        request_sha256 = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        candidate = (
            self.coding_candidate
            if endpoint == "/v1/work-executions"
            else self.interface_candidate
        )
        if (
            receipt.endpoint != endpoint
            or receipt.receipt_id != payload["receipt_id"]
            or receipt.idempotency_key != payload["idempotency_key"]
            or receipt.request_sha256 != request_sha256
            or receipt.candidate_key != candidate.key
            or (
                candidate.selection == "exact"
                and receipt.requested_model != candidate.model_snapshot
            )
        ):
            raise InvariantViolation("PilotHost receipt does not match its request")

    @staticmethod
    def _raise_http(response: httpx.Response) -> None:
        if response.is_success:
            return
        raise InvariantViolation(
            f"production PilotHost HTTP {response.status_code}: {response.text}"
        )
