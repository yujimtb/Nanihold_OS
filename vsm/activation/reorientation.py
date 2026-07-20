from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Protocol

from pydantic import Field, model_validator

from vsm.activation.models import ActivationState, HistoryImportReceipt
from vsm.errors import InvariantViolation
from vsm.ids import new_id
from vsm.interface.models import Commitment, ReadHistoryAction, SubmitReorientationAction
from vsm.interface.service import InterfaceService
from vsm.kernel.models import BlobRef, Identifier, NonBlank, StrictModel, WorkState
from vsm.kernel.service import Kernel
from vsm.pilot.models import EventDeltaSummary, StructuredInterfaceResponse


_UNSTARTED = object()


class HistoryPage(StrictModel):
    result_json: Any
    next_cursor: NonBlank | None
    source_cursor: NonBlank


class HistoryReader(Protocol):
    def list_sessions(self, *, page_cursor: str | None) -> HistoryPage: ...

    def read_timeline(
        self, session_id: str, *, page_cursor: str | None
    ) -> HistoryPage: ...

    def read_raw(self, message_id: str, *, page_cursor: str | None) -> HistoryPage: ...

    def search(self, query: str, *, page_cursor: str | None) -> HistoryPage: ...

    def resolve_reference(
        self, reference_id: str, *, page_cursor: str | None
    ) -> HistoryPage: ...

    def list_open_commitments(self, *, page_cursor: str | None) -> HistoryPage: ...

    def get_current_state(self, *, state_key: str | None, page_cursor: str | None) -> HistoryPage: ...


class HistoryToolResult(StrictModel):
    action_id: Identifier
    operation: NonBlank
    result_json: Any
    result_blob_ref: BlobRef
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    next_cursor: NonBlank | None
    source_cursor: NonBlank
    result_event_id: Identifier
    event_cursor: int = Field(gt=0)


class ReorientationWorkItemSummary(StrictModel):
    """Compact, verified WorkItem state needed to select real resume work."""

    work_item_id: Identifier
    title: NonBlank
    description: NonBlank
    acceptance_criteria: tuple[NonBlank, ...]
    state: WorkState


class ReorientationAssessmentContract(StrictModel):
    """Verified values the Pilot must copy into an Assessment, never infer."""

    import_id: Identifier
    canonical_conversation_id: Identifier
    covered_session_index_ref: NonBlank
    covered_session_count: int = Field(ge=0)
    open_commitment_ids: tuple[Identifier, ...]
    resume_work_items: tuple[ReorientationWorkItemSummary, ...]
    minimum_history_cursor: int = Field(ge=0)


class ReorientationAssessmentContractReference(StrictModel):
    """Compact continuation reference that remains sufficient for submission."""

    import_id: Identifier
    canonical_conversation_id: Identifier
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    covered_session_index_ref: NonBlank
    covered_session_count: int = Field(ge=0)
    open_commitment_ids: tuple[Identifier, ...]
    resume_work_items: tuple[ReorientationWorkItemSummary, ...]
    minimum_history_cursor: int = Field(ge=0)


class SessionIndexSummary(StrictModel):
    session_count: int = Field(ge=0)
    source_kind_counts: dict[NonBlank, int]
    first_message_at: NonBlank | None
    last_message_at: NonBlank | None


class ReorientationTurn(StrictModel):
    provider_session_id: NonBlank | None
    event_delta: EventDeltaSummary
    history_result: HistoryToolResult
    objective: NonBlank
    session_index_ref: NonBlank
    open_commitment_refs: tuple[NonBlank, ...]
    current_state_ref: NonBlank
    assessment_contract: (
        ReorientationAssessmentContract | ReorientationAssessmentContractReference
    )
    audited_history_event_ids: tuple[Identifier, ...]
    session_index_event_ids: tuple[Identifier, ...]
    session_index_summary: SessionIndexSummary
    assessment_contract_included: bool

    @model_validator(mode="after")
    def contract_is_sent_only_on_initial_turn(self) -> "ReorientationTurn":
        is_full = isinstance(self.assessment_contract, ReorientationAssessmentContract)
        if self.provider_session_id is None and (not is_full or not self.assessment_contract_included):
            raise ValueError("initial reorientation turn requires the full assessment contract")
        if self.provider_session_id is not None and (is_full or self.assessment_contract_included):
            raise ValueError("resumed reorientation turn requires only a contract reference")
        return self


class ReorientationPilot(Protocol):
    def respond_reorientation(
        self, context: ReorientationTurn
    ) -> StructuredInterfaceResponse: ...


class HistoryToolService:
    """Typed LETHE history port; Nanihold never scans all history blobs."""

    def __init__(
        self,
        *,
        kernel: Kernel,
        reader: HistoryReader,
        max_result_bytes: int,
    ) -> None:
        if max_result_bytes <= 0:
            raise InvariantViolation("history max_result_bytes must be positive")
        self.kernel = kernel
        self.reader = reader
        self.max_result_bytes = max_result_bytes
        self.session_index_next_cursor: str | None | object = _UNSTARTED
        self.session_index_listed_to_end = False

    def resolve(
        self, action: ReadHistoryAction, *, actor_id: str, idempotency_key: str
    ) -> HistoryToolResult:
        if self.kernel.activation.state is not ActivationState.REORIENTATION_ONLY:
            raise InvariantViolation("history tools require reorientation-only state")
        argument = action.argument
        cursor = action.page_cursor
        if action.operation == "list_sessions":
            raise InvariantViolation("list_sessions is a deterministic reorientation preflight, not a Pilot tool")
        elif action.operation == "read_timeline":
            page = self.reader.read_timeline(
                self._require_argument(action), page_cursor=cursor
            )
        elif action.operation == "read_raw":
            page = self.reader.read_raw(
                self._require_argument(action), page_cursor=cursor
            )
        elif action.operation == "search":
            page = self.reader.search(
                self._require_argument(action), page_cursor=cursor
            )
        elif action.operation == "resolve_reference":
            page = self.reader.resolve_reference(
                self._require_argument(action), page_cursor=cursor
            )
        elif action.operation == "list_open_commitments":
            self._forbid_argument(action)
            page = self.reader.list_open_commitments(page_cursor=cursor)
        elif action.operation == "get_current_state":
            page = self.reader.get_current_state(state_key=argument, page_cursor=cursor)
        else:
            raise InvariantViolation(
                f"unsupported history operation: {action.operation}"
            )
        encoded = json.dumps(
            page.result_json,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > self.max_result_bytes:
            raise InvariantViolation(
                "LETHE history page exceeded max_result_bytes; "
                "implicit truncation is forbidden"
            )
        blob_ref = self.kernel.ledger.put_blob(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        event, event_cursor = self.kernel.activation._record(
            event_type="history_query_resolved",
            payload={
                "action_id": action.action_id,
                "operation": action.operation,
                "argument": argument,
                "page_cursor": cursor,
                "next_cursor": page.next_cursor,
                "source_cursor": page.source_cursor,
                "result_blob_ref": blob_ref,
                "result_sha256": digest,
            },
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.kernel.activation.history_query_operations.add(action.operation)
        self.kernel.activation.history_query_event_ids.add(event.event_id)
        if action.operation == "list_sessions":
            self.session_index_next_cursor = page.next_cursor
            self.session_index_listed_to_end = page.next_cursor is None
        return HistoryToolResult(
            action_id=action.action_id,
            operation=action.operation,
            result_json=page.result_json,
            result_blob_ref=blob_ref,
            result_sha256=digest,
            next_cursor=page.next_cursor,
            source_cursor=page.source_cursor,
            result_event_id=event.event_id,
            event_cursor=event_cursor,
        )

    def _validate_session_index_cursor(self, cursor: str | None) -> None:
        if self.session_index_listed_to_end:
            raise InvariantViolation("list_sessions was already traversed to the final page")
        if self.session_index_next_cursor is _UNSTARTED:
            if cursor is not None:
                raise InvariantViolation("list_sessions traversal must begin at the first page")
            return
        if cursor != self.session_index_next_cursor:
            raise InvariantViolation("list_sessions cursor does not continue the verified traversal")

    def scan_session_index(
        self, *, receipt: HistoryImportReceipt, actor_id: str, idempotency_key: str
    ) -> tuple[tuple[HistoryToolResult, ...], SessionIndexSummary]:
        cursor = None
        observed: set[str] = set()
        pending_pages: list[tuple[str | None, HistoryPage, bytes]] = []
        while True:
            page = self.reader.list_sessions(page_cursor=cursor)
            if not isinstance(page.result_json, list):
                raise InvariantViolation("LETHE list_sessions result must be a list")
            encoded = json.dumps(page.result_json, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            if len(encoded) > self.max_result_bytes:
                raise InvariantViolation("LETHE session index page exceeded max_result_bytes")
            for item in page.result_json:
                if not isinstance(item, dict) or not isinstance(
                    item.get("session_ref"), str
                ):
                    raise InvariantViolation("LETHE session index item is invalid")
                observed.add(item["session_ref"])
            pending_pages.append((cursor, page, encoded))
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        if observed != set(self.kernel.activation.sessions) or len(observed) != receipt.session_count:
            raise InvariantViolation("LETHE session index differs from verified import receipt")

        # LETHE and Nanihold intentionally share one Event Ledger. Appending the
        # audit event for page N before reading page N+1 advances the source
        # snapshot and invalidates LETHE's continuation cursor. Complete and
        # verify the stable read first, then append the per-page audit evidence.
        results: list[HistoryToolResult] = []
        for page_cursor, page, encoded in pending_pages:
            blob_ref = self.kernel.ledger.put_blob(encoded)
            digest = hashlib.sha256(encoded).hexdigest()
            event, event_cursor = self.kernel.activation._record(
                event_type="history_session_index_page_verified",
                payload={
                    "page_cursor": page_cursor,
                    "next_cursor": page.next_cursor,
                    "result_blob_ref": blob_ref,
                    "result_sha256": digest,
                },
                actor_type="system",
                actor_id=actor_id,
                idempotency_key=f"{idempotency_key}:{page_cursor or 'first'}",
            )
            results.append(
                HistoryToolResult(
                    action_id=new_id("action"),
                    operation="list_sessions",
                    result_json=page.result_json,
                    result_blob_ref=blob_ref,
                    result_sha256=digest,
                    next_cursor=page.next_cursor,
                    source_cursor=page.source_cursor,
                    result_event_id=event.event_id,
                    event_cursor=event_cursor,
                )
            )
        self.session_index_listed_to_end = True
        counts = Counter(item.source_kind.value for item in receipt.sessions)
        times = [item.first_message_at for item in receipt.sessions] + [item.last_message_at for item in receipt.sessions]
        return tuple(results), SessionIndexSummary(
            session_count=receipt.session_count,
            source_kind_counts=dict(sorted(counts.items())),
            first_message_at=(None if not times else min(times).isoformat()),
            last_message_at=(None if not times else max(times).isoformat()),
        )

    @staticmethod
    def _require_argument(action: ReadHistoryAction) -> str:
        if action.argument is None:
            raise InvariantViolation(
                f"{action.operation} requires an explicit argument"
            )
        return action.argument

    @staticmethod
    def _forbid_argument(action: ReadHistoryAction) -> None:
        if action.argument is not None:
            raise InvariantViolation(f"{action.operation} does not accept an argument")


class ReorientationService:
    """Bounded Interface Pilot drill-down loop, distinct from normal one-call turns."""

    def __init__(
        self,
        *,
        kernel: Kernel,
        interface: InterfaceService,
        pilot: ReorientationPilot,
        history_reader: HistoryReader,
        max_result_bytes: int,
    ) -> None:
        self.kernel = kernel
        self.interface = interface
        self.pilot = pilot
        self.history = HistoryToolService(
            kernel=kernel,
            reader=history_reader,
            max_result_bytes=max_result_bytes,
        )

    def execute(
        self,
        *,
        initial_action: ReadHistoryAction,
        actor_id: str,
        idempotency_key: str,
        max_tool_rounds: int,
        objective: str,
        session_index_ref: str,
        open_commitment_refs: tuple[str, ...],
        current_state_ref: str,
    ) -> StructuredInterfaceResponse:
        if max_tool_rounds <= 0:
            raise InvariantViolation("max_tool_rounds must be positive")
        if initial_action.operation == "list_sessions":
            raise InvariantViolation("initial reorientation action cannot list_sessions")
        receipt = self.kernel.activation.import_receipt
        if receipt is None:
            raise InvariantViolation("reorientation requires a verified activation contract")
        session_pages, session_summary = self.history.scan_session_index(
            receipt=receipt, actor_id=actor_id, idempotency_key=f"{idempotency_key}:session-index"
        )
        result = self.history.resolve(
            initial_action,
            actor_id=actor_id,
            idempotency_key=f"{idempotency_key}:initial",
        )
        audited_history_event_ids = [result.result_event_id]
        self._materialize_open_commitments(
            actor_id=actor_id,
            idempotency_key=f"{idempotency_key}:commitments",
        )
        provider_session_id = (
            self.kernel.activation.reorientation_provider_session_id
        )
        after_cursor = self.kernel.activation.import_event_cursor
        conversation_id = self.kernel.activation.reorientation_conversation_id
        if receipt is None or conversation_id is None:
            raise InvariantViolation("reorientation requires a verified activation contract")
        resume_work_items = tuple(
            ReorientationWorkItemSummary(
                work_item_id=item.work_item_id,
                title=item.title,
                description=item.description,
                acceptance_criteria=item.acceptance_criteria,
                state=item.state,
            )
            for item in sorted(
                self.kernel.work_items.values(),
                key=lambda work_item: work_item.work_item_id,
            )
        )
        contract = ReorientationAssessmentContract(
            import_id=receipt.inventory_id,
            canonical_conversation_id=conversation_id,
            covered_session_index_ref=receipt.session_index_ref,
            covered_session_count=receipt.session_count,
            open_commitment_ids=tuple(
                sorted(
                    item.commitment_id
                    for item in self.interface.commitments.values()
                    if item.state == "open"
                )
            ),
            resume_work_items=resume_work_items,
            minimum_history_cursor=self.kernel.activation.import_event_cursor,
        )
        for round_index in range(max_tool_rounds):
            response = self.pilot.respond_reorientation(
                ReorientationTurn(
                    provider_session_id=provider_session_id,
                    event_delta=self._delta(after_cursor, result.event_cursor),
                    history_result=result,
                    objective=objective,
                    session_index_ref=session_index_ref,
                    open_commitment_refs=open_commitment_refs,
                    current_state_ref=current_state_ref,
                    assessment_contract=(
                        contract
                        if provider_session_id is None
                        else ReorientationAssessmentContractReference(
                            import_id=contract.import_id,
                            canonical_conversation_id=contract.canonical_conversation_id,
                            contract_sha256=hashlib.sha256(
                                json.dumps(
                                    contract.model_dump(mode="json"),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ).encode("utf-8")
                            ).hexdigest(),
                            covered_session_index_ref=(
                                contract.covered_session_index_ref
                            ),
                            covered_session_count=contract.covered_session_count,
                            open_commitment_ids=contract.open_commitment_ids,
                            resume_work_items=contract.resume_work_items,
                            minimum_history_cursor=contract.minimum_history_cursor,
                        )
                    ),
                    audited_history_event_ids=tuple(audited_history_event_ids),
                    session_index_event_ids=tuple(item.result_event_id for item in session_pages),
                    session_index_summary=session_summary,
                    assessment_contract_included=provider_session_id is None,
                )
            )
            self.kernel.activation.record_reorientation_session_checkpoint(
                provider_session_id=response.provider_session_id,
                actor_id=actor_id,
                idempotency_key=f"{idempotency_key}:session-checkpoint:{round_index}",
            )
            self.kernel.activation.record_reorientation_usage(
                input_tokens=response.pilot_usage.input_tokens,
                output_tokens=response.pilot_usage.output_tokens,
                actor_id=actor_id,
                idempotency_key=f"{idempotency_key}:usage:{round_index}",
            )
            provider_session_id = response.provider_session_id
            history_actions = [
                action
                for action in response.actions
                if isinstance(action, ReadHistoryAction)
            ]
            submissions = [
                action
                for action in response.actions
                if isinstance(action, SubmitReorientationAction)
            ]
            if submissions:
                if len(response.actions) != 1 or len(submissions) != 1:
                    raise InvariantViolation(
                        "reorientation submission must be the only action"
                    )
                if provider_session_id is None:
                    raise InvariantViolation(
                        "reorientation assessment lacks a provider session"
                    )
                if (
                    submissions[0].assessment.conversation_id
                    not in self.interface.conversations
                ):
                    raise InvariantViolation(
                        "reorientation assessment references no canonical Conversation"
                    )
                self.kernel.activation.submit_assessment(
                    submissions[0].assessment,
                    open_commitment_ids=(
                        item.commitment_id
                        for item in self.interface.commitments.values()
                        if item.state == "open"
                    ),
                    existing_work_item_ids=self.kernel.work_items,
                    session_index_listed_to_end=self.history.session_index_listed_to_end,
                    actor_id=actor_id,
                    idempotency_key=f"{idempotency_key}:assessment",
                )
                self.interface.materialize_reorientation_assessment(
                    assessment=submissions[0].assessment,
                    actor_id=actor_id,
                    idempotency_key=f"{idempotency_key}:materialize",
                )
                self.interface.record_reorientation_session(
                    conversation_id=submissions[0].assessment.conversation_id,
                    provider_session_id=provider_session_id,
                    actor_id=actor_id,
                    idempotency_key=f"{idempotency_key}:session",
                )
                return response
            if len(response.actions) != 1 or len(history_actions) != 1:
                raise InvariantViolation(
                    "reorientation response must request one history query or submit"
                )
            after_cursor = result.event_cursor
            result = self.history.resolve(
                history_actions[0],
                actor_id=actor_id,
                idempotency_key=f"{idempotency_key}:round:{round_index}",
            )
            audited_history_event_ids.append(result.result_event_id)
        raise InvariantViolation("reorientation exceeded explicit history tool budget")

    def _materialize_open_commitments(
        self, *, actor_id: str, idempotency_key: str
    ) -> None:
        conversation_id = self.kernel.activation.reorientation_conversation_id
        if conversation_id is None:
            raise InvariantViolation("history commitments require canonical Conversation")
        page_cursor = None
        commitments: list[Commitment] = []
        result_event_ids: list[str] = []
        while True:
            result = self.history.resolve(
                ReadHistoryAction(
                    action_id=new_id("action"),
                    kind="history.read",
                    operation="list_open_commitments",
                    argument=None,
                    page_cursor=page_cursor,
                ),
                actor_id=actor_id,
                idempotency_key=f"{idempotency_key}:{page_cursor or 'first'}",
            )
            if not isinstance(result.result_json, list):
                raise InvariantViolation("LETHE open commitments result must be a list")
            for item in result.result_json:
                if not isinstance(item, dict):
                    raise InvariantViolation("LETHE open commitment must be an object")
                commitment_id = item.get("commitment_id")
                statement = item.get("text")
                if not isinstance(commitment_id, str) or not isinstance(statement, str):
                    raise InvariantViolation("LETHE open commitment fields are invalid")
                commitments.append(
                    Commitment(
                        commitment_id=commitment_id,
                        conversation_id=conversation_id,
                        statement=statement,
                        work_item_id=None,
                        state="open",
                    )
                )
            result_event_ids.append(result.result_event_id)
            if result.next_cursor is None:
                break
            page_cursor = result.next_cursor
        self.interface.materialize_history_commitments(
            conversation_id=conversation_id,
            commitments=tuple(commitments),
            history_result_event_ids=tuple(result_event_ids),
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    def _delta(self, after_cursor: int, through_cursor: int) -> EventDeltaSummary:
        cursor = after_cursor
        counts: Counter[str] = Counter()
        streams: set[str] = set()
        while cursor < through_cursor:
            page = self.kernel.ledger.page(
                cursor, min(500, through_cursor - cursor)
            )
            if not page:
                raise InvariantViolation("reorientation Event Ledger delta ended early")
            for stored in page:
                if stored.cursor > through_cursor:
                    break
                counts[stored.event.event_type] += 1
                streams.add(stored.event.stream_id)
                cursor = stored.cursor
        return EventDeltaSummary(
            after_cursor=after_cursor,
            through_cursor=through_cursor,
            event_count=sum(counts.values()),
            event_type_counts=dict(sorted(counts.items())),
            changed_stream_ids=tuple(sorted(streams)),
        )
