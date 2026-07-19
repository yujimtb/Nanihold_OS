from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Protocol

from pydantic import Field

from vsm.activation.models import ActivationState
from vsm.errors import InvariantViolation
from vsm.interface.models import ReadHistoryAction, SubmitReorientationAction
from vsm.interface.service import InterfaceService
from vsm.kernel.models import BlobRef, Identifier, NonBlank, StrictModel
from vsm.kernel.service import Kernel
from vsm.pilot.models import EventDeltaSummary, StructuredInterfaceResponse


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

    def get_current_state(self, *, page_cursor: str | None) -> HistoryPage: ...


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


class ReorientationTurn(StrictModel):
    provider_session_id: NonBlank | None
    event_delta: EventDeltaSummary
    history_result: HistoryToolResult
    objective: NonBlank
    session_index_ref: NonBlank
    open_commitment_refs: tuple[Identifier, ...]
    current_state_ref: NonBlank


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

    def resolve(
        self, action: ReadHistoryAction, *, actor_id: str, idempotency_key: str
    ) -> HistoryToolResult:
        if self.kernel.activation.state is not ActivationState.REORIENTATION_ONLY:
            raise InvariantViolation("history tools require reorientation-only state")
        argument = action.argument
        cursor = action.page_cursor
        if action.operation == "list_sessions":
            self._forbid_argument(action)
            page = self.reader.list_sessions(page_cursor=cursor)
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
            self._forbid_argument(action)
            page = self.reader.get_current_state(page_cursor=cursor)
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
    """Bounded Fable drill-down loop, distinct from normal one-call turns."""

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
        result = self.history.resolve(
            initial_action,
            actor_id=actor_id,
            idempotency_key=f"{idempotency_key}:initial",
        )
        provider_session_id = None
        after_cursor = self.kernel.activation.import_event_cursor
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
                )
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
                    actor_id=actor_id,
                    idempotency_key=f"{idempotency_key}:assessment",
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
        raise InvariantViolation("reorientation exceeded explicit history tool budget")

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
