from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Literal

from vsm.activation.models import (
    REQUIRED_HISTORY_SOURCE_KINDS,
    ActivationState,
    ActivationStatus,
    HistoryImportReceipt,
    HistorySession,
    ReorientationAssessment,
    ReorientationInterruptionReason,
    ReorientationRevisionReason,
)
from vsm.errors import InvariantViolation
from vsm.ids import deterministic_event_id
from vsm.kernel.ledger import OperationalLedger
from vsm.kernel.models import EventEnvelope


class ActivationService:
    """Fail-closed commissioning state for the owner Interface Node."""

    def __init__(
        self,
        *,
        data_space_id: str,
        ledger: OperationalLedger,
        clock: Callable[[], datetime],
    ) -> None:
        self.data_space_id = data_space_id
        self.ledger = ledger
        self.clock = clock
        self.state = ActivationState.UNCOMMISSIONED
        self.import_receipt: HistoryImportReceipt | None = None
        self.sessions: dict[str, HistorySession] = {}
        self.assessment: ReorientationAssessment | None = None
        self.approved_at: datetime | None = None
        self._stream_id = f"activation:{data_space_id.split(':', 1)[-1]}"
        self._version = 0
        self.import_event_cursor = 0
        self.history_query_operations: set[str] = set()
        self.history_query_event_ids: set[str] = set()
        self.reorientation_pilot_calls = 0
        self.reorientation_input_tokens = 0
        self.reorientation_output_tokens = 0
        self.reorientation_error: str | None = None
        self.reorientation_attempt_in_progress = False
        self.reorientation_attempt_started_stream_version: int | None = None
        self.pending_reorientation_revision_reason: (
            ReorientationRevisionReason | None
        ) = None
        self.work_graph_snapshot_id: str | None = None
        self.reorientation_conversation_id: str | None = None
        self.reorientation_provider_session_id: str | None = None

    def _record(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        actor_type: str,
        actor_id: str | None,
        idempotency_key: str,
    ) -> tuple[EventEnvelope, int]:
        event = EventEnvelope(
            event_id=deterministic_event_id(
                data_space_id=self.data_space_id,
                stream_id=self._stream_id,
                idempotency_key=idempotency_key,
            ),
            data_space_id=self.data_space_id,
            stream_id=self._stream_id,
            stream_version=self._version + 1,
            event_type=event_type,
            occurred_at=self.clock(),
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=self._stream_id,
            causation_id=None,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        result = self.ledger.append(event, self._version)
        self._version = result.stream_version
        return event, result.cursor

    def register_history_import(
        self,
        receipt: HistoryImportReceipt,
        *,
        reorientation_conversation_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        try:
            receipt = HistoryImportReceipt.model_validate(
                receipt.model_dump(mode="json")
            )
        except Exception as exc:
            raise InvariantViolation(
                "HistoryImportReceipt violates the LETHE handoff contract"
            ) from exc
        if self.state is not ActivationState.UNCOMMISSIONED:
            if receipt == self.import_receipt:
                return
            raise InvariantViolation(
                "history import is accepted only while uncommissioned"
            )
        if self.work_graph_snapshot_id is None:
            raise InvariantViolation(
                "current Work Graph must be imported before history"
            )
        if not reorientation_conversation_id:
            raise InvariantViolation("reorientation requires a canonical Conversation")
        if receipt.data_space_id != self.data_space_id:
            raise InvariantViolation("history import DataSpace mismatch")
        kinds = {source.source_kind for source in receipt.sources}
        if kinds != REQUIRED_HISTORY_SOURCE_KINDS:
            missing = sorted(
                kind.value for kind in REQUIRED_HISTORY_SOURCE_KINDS - kinds
            )
            extra = sorted(kind.value for kind in kinds - REQUIRED_HISTORY_SOURCE_KINDS)
            raise InvariantViolation(
                f"history source coverage mismatch; missing={missing}, extra={extra}"
            )
        sessions = {session.session_ref: session for session in receipt.sessions}
        if len(sessions) != receipt.session_count:
            raise InvariantViolation(
                "history session identities must be globally unique"
            )
        _, cursor = self._record(
            event_type="history_import_verified",
            payload={
                "receipt": receipt.model_dump(mode="json"),
                "reorientation_conversation_id": reorientation_conversation_id,
                "state": ActivationState.HISTORY_IMPORTED,
            },
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.import_receipt = receipt
        self.reorientation_conversation_id = reorientation_conversation_id
        self.sessions = sessions
        self.state = ActivationState.HISTORY_IMPORTED
        self.import_event_cursor = cursor

    def start_reorientation(self, *, actor_id: str, idempotency_key: str) -> None:
        if self.reorientation_attempt_in_progress:
            raise InvariantViolation("a reorientation attempt is already in progress")
        if self.state is ActivationState.HISTORY_IMPORTED:
            event_type = "reorientation_started"
            payload: dict[str, object] = {"state": ActivationState.REORIENTATION_ONLY}
        elif (
            self.state is ActivationState.REORIENTATION_ONLY
            and self.reorientation_error is not None
        ):
            event_type = "reorientation_retry_started"
            payload = {
                "state": ActivationState.REORIENTATION_ONLY,
                "retry_reason": "failure",
                "prior_error_code": self.reorientation_error,
            }
        elif (
            self.state is ActivationState.REORIENTATION_ONLY
            and self.pending_reorientation_revision_reason is not None
        ):
            event_type = "reorientation_revision_retry_started"
            payload = {
                "state": ActivationState.REORIENTATION_ONLY,
                "retry_reason": "assessment_revision",
                "revision_reason_code": self.pending_reorientation_revision_reason,
            }
        else:
            raise InvariantViolation("reorientation requires a verified history import")
        started_event, _ = self._record(
            event_type=event_type,
            payload=payload,
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.state = ActivationState.REORIENTATION_ONLY
        self.reorientation_error = None
        self.pending_reorientation_revision_reason = None
        self.reorientation_attempt_in_progress = True
        self.reorientation_attempt_started_stream_version = started_event.stream_version

    def record_reorientation_failure(
        self, *, error_code: str, actor_id: str, idempotency_key: str
    ) -> None:
        if self.state is not ActivationState.REORIENTATION_ONLY:
            raise InvariantViolation(
                "reorientation failure requires reorientation-only state"
            )
        if not self.reorientation_attempt_in_progress:
            raise InvariantViolation(
                "reorientation failure requires an attempt in progress"
            )
        self._record(
            event_type="reorientation_failed",
            payload={"error_code": error_code},
            actor_type="system",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.reorientation_error = error_code
        self.pending_reorientation_revision_reason = None
        self.reorientation_attempt_in_progress = False
        self.reorientation_attempt_started_stream_version = None

    def interrupt_reorientation_attempt(
        self,
        *,
        reason_code: ReorientationInterruptionReason,
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        if self.state is not ActivationState.REORIENTATION_ONLY:
            raise InvariantViolation(
                "reorientation interruption requires reorientation-only state"
            )
        if not self.reorientation_attempt_in_progress:
            raise InvariantViolation(
                "reorientation interruption requires an attempt in progress"
            )
        try:
            reason_code = ReorientationInterruptionReason(reason_code)
        except (TypeError, ValueError) as exc:
            raise InvariantViolation(
                "reorientation interruption reason code is not allowed"
            ) from exc
        self._record(
            event_type="reorientation_attempt_interrupted",
            payload={"reason_code": reason_code},
            actor_type="system",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.reorientation_error = reason_code
        self.pending_reorientation_revision_reason = None
        self.reorientation_attempt_in_progress = False
        self.reorientation_attempt_started_stream_version = None

    def record_reorientation_session_checkpoint(
        self,
        *,
        provider_session_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        if self.state is not ActivationState.REORIENTATION_ONLY:
            raise InvariantViolation(
                "reorientation session checkpoint requires reorientation-only state"
            )
        if not self.reorientation_attempt_in_progress:
            raise InvariantViolation(
                "reorientation session checkpoint requires an attempt in progress"
            )
        if not provider_session_id:
            raise InvariantViolation(
                "reorientation session checkpoint requires a provider session"
            )
        self._record(
            event_type="reorientation_session_checkpointed",
            payload={"provider_session_id": provider_session_id},
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.reorientation_provider_session_id = provider_session_id

    def submit_assessment(
        self,
        assessment: ReorientationAssessment,
        *,
        open_commitment_ids: Iterable[str],
        existing_work_item_ids: Iterable[str],
        session_index_listed_to_end: bool,
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        if self.state is not ActivationState.REORIENTATION_ONLY:
            raise InvariantViolation("assessment requires reorientation-only state")
        if not self.reorientation_attempt_in_progress:
            raise InvariantViolation(
                "assessment requires a reorientation attempt in progress"
            )
        if (
            self.import_receipt is None
            or assessment.import_id != self.import_receipt.inventory_id
        ):
            raise InvariantViolation(
                "assessment does not reference the verified import"
            )
        if assessment.conversation_id != self.reorientation_conversation_id:
            raise InvariantViolation(
                "assessment does not reference the imported canonical Conversation"
            )
        if (
            assessment.covered_session_index_ref
            != self.import_receipt.session_index_ref
        ):
            raise InvariantViolation(
                "assessment session index differs from verified import"
            )
        if assessment.covered_session_count != self.import_receipt.session_count:
            raise InvariantViolation(
                "assessment session count differs from verified import"
            )
        if not session_index_listed_to_end:
            raise InvariantViolation(
                "assessment requires a complete list_sessions traversal"
            )
        if set(assessment.open_commitment_ids) != set(open_commitment_ids):
            raise InvariantViolation(
                "assessment open commitments are incomplete or stale"
            )
        existing_work_item_id_set = set(existing_work_item_ids)
        if existing_work_item_id_set and not assessment.resume_work_item_ids:
            raise InvariantViolation(
                "assessment must select at least one current WorkItem for resume"
            )
        if not set(assessment.resume_work_item_ids).issubset(existing_work_item_id_set):
            raise InvariantViolation("assessment references an unknown resume WorkItem")
        if assessment.history_cursor < self.import_event_cursor:
            raise InvariantViolation(
                "assessment history cursor predates verified import"
            )
        if assessment.current_state_cursor < assessment.history_cursor:
            raise InvariantViolation("current state must be read after history")
        if "get_current_state" not in self.history_query_operations:
            raise InvariantViolation(
                "assessment requires a current-state history query"
            )
        required_claims = {"understanding"}
        required_claims.update(
            f"active_missions:{index}"
            for index in range(len(assessment.active_missions))
        )
        required_claims.update(
            f"decisions_and_constraints:{index}"
            for index in range(len(assessment.decisions_and_constraints))
        )
        cited_claims = {citation.claim_ref for citation in assessment.citations}
        if not required_claims.issubset(cited_claims):
            raise InvariantViolation(
                "assessment contains claims without evidence citations"
            )
        if any(
            citation.evidence_ref not in self.history_query_event_ids
            for citation in assessment.citations
        ):
            raise InvariantViolation(
                "assessment citations must reference audited history query results"
            )
        self._record(
            event_type="reorientation_assessment_accepted",
            payload={
                "assessment": assessment.model_dump(mode="json"),
                "state": ActivationState.AWAITING_OWNER_CONFIRMATION,
            },
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.assessment = assessment
        self.state = ActivationState.AWAITING_OWNER_CONFIRMATION
        self.pending_reorientation_revision_reason = None
        self.reorientation_attempt_in_progress = False
        self.reorientation_attempt_started_stream_version = None

    def request_assessment_revision(
        self,
        reason_code: ReorientationRevisionReason,
        *,
        requested_by: Literal["owner", "system"],
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        if self.state is not ActivationState.AWAITING_OWNER_CONFIRMATION:
            raise InvariantViolation(
                "assessment revision requires awaiting-owner-confirmation state"
            )
        if self.assessment is None:
            raise InvariantViolation(
                "assessment revision requires an accepted assessment"
            )
        try:
            reason_code = ReorientationRevisionReason(reason_code)
        except (TypeError, ValueError) as exc:
            raise InvariantViolation(
                "assessment revision reason code is not allowed"
            ) from exc
        if requested_by not in ("owner", "system"):
            raise InvariantViolation(
                "assessment revision requester must be owner or system"
            )
        prior_assessment_id = self.assessment.assessment_id
        self._record(
            event_type="reorientation_assessment_revision_requested",
            payload={
                "prior_assessment_id": prior_assessment_id,
                "reason_code": reason_code,
                "state": ActivationState.REORIENTATION_ONLY,
            },
            actor_type="human" if requested_by == "owner" else "system",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.assessment = None
        self.reorientation_error = None
        self.pending_reorientation_revision_reason = reason_code
        self.reorientation_attempt_in_progress = False
        self.reorientation_attempt_started_stream_version = None
        self.state = ActivationState.REORIENTATION_ONLY

    def approve(
        self,
        assessment_id: str,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        if self.state is not ActivationState.AWAITING_OWNER_CONFIRMATION:
            raise InvariantViolation("activation requires owner confirmation")
        if self.assessment is None or self.assessment.assessment_id != assessment_id:
            raise InvariantViolation("owner confirmation references unknown assessment")
        approved_at = self.clock()
        self._record(
            event_type="activation_approved",
            payload={
                "assessment_id": assessment_id,
                "approved_at": approved_at.isoformat(),
                "state": ActivationState.ACTIVE,
            },
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.approved_at = approved_at
        self.state = ActivationState.ACTIVE

    def require_active(self, operation: str) -> None:
        if self.state is not ActivationState.ACTIVE:
            raise InvariantViolation(
                f"{operation} is forbidden before owner-confirmed activation "
                f"(state={self.state})"
            )

    def status(self) -> ActivationStatus:
        return ActivationStatus(
            state=self.state,
            import_receipt=self.import_receipt,
            assessment=self.assessment,
            approved_at=self.approved_at,
            status_model_calls=0,
            reorientation_attempt_in_progress=self.reorientation_attempt_in_progress,
            pending_reorientation_revision_reason=(
                self.pending_reorientation_revision_reason
            ),
            reorientation_pilot_calls=self.reorientation_pilot_calls,
            reorientation_input_tokens=self.reorientation_input_tokens,
            reorientation_output_tokens=self.reorientation_output_tokens,
            reorientation_error=self.reorientation_error,
            work_graph_snapshot_id=self.work_graph_snapshot_id,
            reorientation_conversation_id=self.reorientation_conversation_id,
        )

    def record_reorientation_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        if input_tokens < 0 or output_tokens < 0:
            raise InvariantViolation("reorientation token usage cannot be negative")
        if (
            self.state is not ActivationState.REORIENTATION_ONLY
            or not self.reorientation_attempt_in_progress
        ):
            raise InvariantViolation(
                "reorientation usage requires an attempt in progress"
            )
        self._record(
            event_type="reorientation_pilot_usage_recorded",
            payload={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.reorientation_pilot_calls += 1
        self.reorientation_input_tokens += input_tokens
        self.reorientation_output_tokens += output_tokens
