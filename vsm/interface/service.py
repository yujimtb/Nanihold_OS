from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from vsm.errors import InvariantViolation
from vsm.ids import new_id
from vsm.interface.models import (
    Commitment,
    Conversation,
    ConversationMessage,
    ConversationStatus,
    Decision,
    NodeMemory,
)
from vsm.kernel.ledger import OperationalLedger
from vsm.kernel.models import EventEnvelope, ExecutionState, WorkState
from vsm.kernel.service import Kernel
from vsm.pilot.models import (
    EventDeltaSummary,
    InterfaceResumePack,
    InterfaceTurn,
    StructuredInterfaceResponse,
)


class InterfacePilot(Protocol):
    def respond(
        self, *, owner_text: str, context: InterfaceTurn
    ) -> StructuredInterfaceResponse: ...


class InterfaceService:
    """Persistent owner interface with one expensive Pilot call per normal turn."""

    def __init__(
        self,
        *,
        kernel: Kernel,
        ledger: OperationalLedger,
        pilot: InterfacePilot,
        clock: Callable[[], datetime],
    ) -> None:
        self.kernel = kernel
        self.ledger = ledger
        self.pilot = pilot
        self.clock = clock
        self.conversations: dict[str, Conversation] = {}
        self.messages: dict[str, list[ConversationMessage]] = {}
        self.commitments: dict[str, Commitment] = {}
        self.decisions: dict[str, Decision] = {}
        self.node_memories: dict[str, NodeMemory] = {}
        self._versions: dict[str, int] = {}

    def _record(
        self,
        *,
        conversation_id: str,
        event_type: str,
        payload: dict[str, object],
        actor_type: str,
        actor_id: str | None,
        idempotency_key: str,
    ) -> int:
        expected = self._versions.get(conversation_id, 0)
        event = EventEnvelope(
            event_id=new_id("event"),
            data_space_id=self.kernel.data_space.data_space_id,
            stream_id=conversation_id,
            stream_version=expected + 1,
            event_type=event_type,
            occurred_at=self.clock(),
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=conversation_id,
            causation_id=None,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        result = self.ledger.append(event, expected)
        self._versions[conversation_id] = result.stream_version
        return result.cursor

    def create_conversation(
        self,
        conversation: Conversation,
        *,
        idempotency_key: str,
    ) -> Conversation:
        node = self.kernel.nodes.get(conversation.interface_node_id)
        if node is None or node.kind != "interface":
            raise InvariantViolation("conversation requires an owner Interface Node")
        if conversation.data_space_id != self.kernel.data_space.data_space_id:
            raise InvariantViolation("Conversation DataSpace mismatch")
        cursor = self._record(
            conversation_id=conversation.conversation_id,
            event_type="conversation_created",
            payload={"conversation": conversation.model_dump(mode="json")},
            actor_type="human",
            actor_id=conversation.owner_id,
            idempotency_key=idempotency_key,
        )
        stored = conversation.model_copy(update={"last_event_cursor": cursor})
        self.conversations[conversation.conversation_id] = stored
        self.messages[conversation.conversation_id] = []
        return stored

    def turn(
        self,
        *,
        conversation_id: str,
        owner_text: str,
        idempotency_key: str,
        force_new_pilot: bool,
    ) -> StructuredInterfaceResponse:
        conversation = self.conversations.get(conversation_id)
        if conversation is None or conversation.status != "active":
            raise InvariantViolation("active Conversation not found")
        if not owner_text.strip():
            raise InvariantViolation("owner message must not be blank")

        # Persistence is deliberately first. If LETHE fails, the Pilot is never called.
        blob_ref = self.ledger.put_blob(owner_text.encode("utf-8"))
        owner_message = ConversationMessage(
            message_id=new_id("message"),
            conversation_id=conversation_id,
            role="owner",
            display_text=None,
            blob_ref=blob_ref,
            occurred_at=self.clock(),
        )
        cursor = self._record(
            conversation_id=conversation_id,
            event_type="owner_message_received",
            payload={"message": owner_message.model_dump(mode="json")},
            actor_type="human",
            actor_id=conversation.owner_id,
            idempotency_key=f"{idempotency_key}:owner",
        )
        self.messages[conversation_id].append(owner_message)

        unfinished = tuple(
            work
            for work in self.kernel.work_items.values()
            if work.owner_node_id == conversation.interface_node_id
            and work.state not in (WorkState.COMPLETED, WorkState.CANCELLED)
        )
        open_commitments = tuple(
            item
            for item in self.commitments.values()
            if item.conversation_id == conversation_id and item.state == "open"
        )
        delta = self._event_delta(
            after_cursor=conversation.last_event_cursor,
            through_cursor=cursor,
        )
        needs_resume_pack = force_new_pilot or conversation.provider_session_id is None
        resume_pack = None
        if needs_resume_pack:
            superseded = {
                decision.supersedes_decision_id
                for decision in self.decisions.values()
                if decision.supersedes_decision_id is not None
            }
            resume_pack = InterfaceResumePack(
                node_memory=tuple(
                    memory.model_dump(mode="json")
                    for memory in self.node_memories.values()
                    if memory.node_id == conversation.interface_node_id
                ),
                unfinished_work_items=tuple(
                    work.model_dump(mode="json") for work in unfinished
                ),
                open_commitments=tuple(
                    commitment.model_dump(mode="json")
                    for commitment in open_commitments
                ),
                active_decisions=tuple(
                    decision.model_dump(mode="json")
                    for decision in self.decisions.values()
                    if (
                        decision.conversation_id == conversation_id
                        and decision.decision_id not in superseded
                    )
                ),
            )
        context = InterfaceTurn(
            owner_message_blob_ref=blob_ref,
            event_delta=delta,
            resume_pack=resume_pack,
            provider_session_id=None
            if needs_resume_pack
            else conversation.provider_session_id,
        )

        # Exactly one Interface Pilot call. Display, directives, decisions, and
        # commitments must arrive together; no summarizer call is permitted.
        response = self.pilot.respond(owner_text=owner_text, context=context)
        interface_message = ConversationMessage(
            message_id=new_id("message"),
            conversation_id=conversation_id,
            role="interface",
            display_text=response.display_text,
            blob_ref=None,
            occurred_at=self.clock(),
        )
        response_cursor = self._record(
            conversation_id=conversation_id,
            event_type="interface_response_recorded",
            payload={
                "message": interface_message.model_dump(mode="json"),
                "work_directives": list(response.work_directives),
                "decisions": list(response.decisions),
                "commitment_updates": list(response.commitment_updates),
                "provider_session_id": response.provider_session_id,
                "pilot_usage": response.pilot_usage.model_dump(mode="json"),
                "context_mode": "resume_pack" if force_new_pilot else "provider_delta",
            },
            actor_type="pilot",
            actor_id=conversation.interface_node_id,
            idempotency_key=f"{idempotency_key}:response",
        )
        self.messages[conversation_id].append(interface_message)
        self.conversations[conversation_id] = conversation.model_copy(
            update={
                "provider_session_id": response.provider_session_id,
                "last_event_cursor": response_cursor,
            }
        )
        self._apply_structured_updates(conversation_id, response)
        return response

    def _event_delta(
        self, *, after_cursor: int, through_cursor: int
    ) -> EventDeltaSummary:
        if through_cursor < after_cursor:
            raise InvariantViolation("Event delta cursor moved backwards")
        cursor = after_cursor
        event_types: Counter[str] = Counter()
        changed_streams: set[str] = set()
        event_count = 0
        while cursor < through_cursor:
            page_start = cursor
            page = self.ledger.page(cursor, min(500, through_cursor - cursor))
            if not page:
                raise InvariantViolation("Event Ledger ended before owner message cursor")
            for stored in page:
                if stored.cursor > through_cursor:
                    break
                event_count += 1
                event_types[stored.event.event_type] += 1
                changed_streams.add(stored.event.stream_id)
                cursor = stored.cursor
            if cursor <= page_start:
                raise InvariantViolation("Event Ledger delta did not advance")
        return EventDeltaSummary(
            after_cursor=after_cursor,
            through_cursor=through_cursor,
            event_count=event_count,
            event_type_counts=dict(sorted(event_types.items())),
            changed_stream_ids=tuple(sorted(changed_streams)),
        )

    def _apply_structured_updates(
        self, conversation_id: str, response: StructuredInterfaceResponse
    ) -> None:
        for raw in response.decisions:
            decision = Decision.model_validate(
                {
                    "decision_id": raw.get("decision_id") or new_id("decision"),
                    "conversation_id": conversation_id,
                    "statement": raw["statement"],
                    "supersedes_decision_id": raw.get("supersedes_decision_id"),
                }
            )
            self.decisions[decision.decision_id] = decision
        for raw in response.commitment_updates:
            commitment_id = raw.get("commitment_id") or new_id("commitment")
            commitment = Commitment.model_validate(
                {
                    "commitment_id": commitment_id,
                    "conversation_id": conversation_id,
                    "statement": raw["statement"],
                    "work_item_id": raw.get("work_item_id"),
                    "state": raw["state"],
                }
            )
            self.commitments[commitment.commitment_id] = commitment

    def status(self, conversation_id: str) -> ConversationStatus:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise InvariantViolation("Conversation not found")
        return ConversationStatus(
            conversation=conversation,
            open_commitments=tuple(
                commitment
                for commitment in self.commitments.values()
                if commitment.conversation_id == conversation_id
                and commitment.state == "open"
            ),
            unfinished_work_item_ids=tuple(
                work.work_item_id
                for work in self.kernel.work_items.values()
                if work.owner_node_id == conversation.interface_node_id
                and work.state not in (WorkState.COMPLETED, WorkState.CANCELLED)
            ),
            active_execution_ids=tuple(
                execution.execution_id
                for execution in self.kernel.executions.values()
                if execution.state is ExecutionState.ACTIVE
            ),
            last_messages=tuple(self.messages[conversation_id][-20:]),
            model_calls=0,
        )
