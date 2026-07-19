from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from vsm.activation.models import (
    REQUIRED_HISTORY_SOURCE_KINDS,
    ActivationState,
    ActivationStatus,
    HistoryImportReceipt,
    HistorySession,
    ReorientationAssessment,
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
        self.work_graph_snapshot_id: str | None = None

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
            raise InvariantViolation("history import is accepted only while uncommissioned")
        if self.work_graph_snapshot_id is None:
            raise InvariantViolation(
                "current Work Graph must be imported before history"
            )
        if (
            receipt.data_space_id != self.data_space_id
        ):
            raise InvariantViolation("history import DataSpace mismatch")
        kinds = {source.source_kind for source in receipt.sources}
        if kinds != REQUIRED_HISTORY_SOURCE_KINDS:
            missing = sorted(kind.value for kind in REQUIRED_HISTORY_SOURCE_KINDS - kinds)
            extra = sorted(kind.value for kind in kinds - REQUIRED_HISTORY_SOURCE_KINDS)
            raise InvariantViolation(
                f"history source coverage mismatch; missing={missing}, extra={extra}"
            )
        sessions = {
            session.session_ref: session
            for session in receipt.sessions
        }
        if len(sessions) != receipt.session_count:
            raise InvariantViolation("history session identities must be globally unique")
        _, cursor = self._record(
            event_type="history_import_verified",
            payload={
                "receipt": receipt.model_dump(mode="json"),
                "state": ActivationState.HISTORY_IMPORTED,
            },
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.import_receipt = receipt
        self.sessions = sessions
        self.state = ActivationState.HISTORY_IMPORTED
        self.import_event_cursor = cursor

    def start_reorientation(
        self, *, actor_id: str, idempotency_key: str
    ) -> None:
        retrying = (
            self.state is ActivationState.REORIENTATION_ONLY
            and self.reorientation_error is not None
        )
        if self.state is not ActivationState.HISTORY_IMPORTED and not retrying:
            raise InvariantViolation("reorientation requires a verified history import")
        self._record(
            event_type=(
                "reorientation_retry_started"
                if retrying
                else "reorientation_started"
            ),
            payload={"state": ActivationState.REORIENTATION_ONLY},
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.state = ActivationState.REORIENTATION_ONLY
        self.reorientation_error = None

    def record_reorientation_failure(
        self, *, error_code: str, actor_id: str, idempotency_key: str
    ) -> None:
        if self.state is not ActivationState.REORIENTATION_ONLY:
            raise InvariantViolation(
                "reorientation failure requires reorientation-only state"
            )
        self._record(
            event_type="reorientation_failed",
            payload={"error_code": error_code},
            actor_type="system",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.reorientation_error = error_code

    def submit_assessment(
        self,
        assessment: ReorientationAssessment,
        *,
        open_commitment_ids: Iterable[str],
        existing_work_item_ids: Iterable[str],
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        if self.state is not ActivationState.REORIENTATION_ONLY:
            raise InvariantViolation("assessment requires reorientation-only state")
        if (
            self.import_receipt is None
            or assessment.import_id != self.import_receipt.inventory_id
        ):
            raise InvariantViolation("assessment does not reference the verified import")
        if set(assessment.covered_session_ids) != set(self.sessions):
            raise InvariantViolation("assessment must cover every imported session")
        if set(assessment.open_commitment_ids) != set(open_commitment_ids):
            raise InvariantViolation("assessment open commitments are incomplete or stale")
        if not set(assessment.resume_work_item_ids).issubset(set(existing_work_item_ids)):
            raise InvariantViolation("assessment references an unknown resume WorkItem")
        if assessment.history_cursor < self.import_event_cursor:
            raise InvariantViolation("assessment history cursor predates verified import")
        if assessment.current_state_cursor < assessment.history_cursor:
            raise InvariantViolation("current state must be read after history")
        if "get_current_state" not in self.history_query_operations:
            raise InvariantViolation("assessment requires a current-state history query")
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
            raise InvariantViolation("assessment contains claims without evidence citations")
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
            reorientation_pilot_calls=self.reorientation_pilot_calls,
            reorientation_input_tokens=self.reorientation_input_tokens,
            reorientation_output_tokens=self.reorientation_output_tokens,
            reorientation_error=self.reorientation_error,
            work_graph_snapshot_id=self.work_graph_snapshot_id,
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
