from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from vsm.errors import InvariantViolation
from vsm.ids import deterministic_event_id, new_id
from vsm.interface.models import (
    Commitment,
    Conversation,
    ConversationActionReceipt,
    ConversationCreatedReceipt,
    ConversationMessage,
    ConversationStatus,
    CreateWorkItemAction,
    Decision,
    DelegateWorkItemAction,
    NodeMemory,
    OwnerMessageAction,
    PilotSession,
    PlanEffectAction,
    ProposeCompletionAction,
    RecordDecisionAction,
    SelectPilotAction,
    SurfaceBinding,
    UpdateCommitmentAction,
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
from vsm.token_lab.lab import (
    TokenIncidentKind,
    TokenLabEventService,
    TokenObservation,
)

if TYPE_CHECKING:
    from vsm.activation.models import ReorientationAssessment


class InterfacePilot(Protocol):
    def respond(
        self, *, owner_text: str, context: InterfaceTurn
    ) -> StructuredInterfaceResponse: ...


class InterfaceService:
    """Canonical conversation boundary with durable action reconciliation."""

    def __init__(
        self,
        *,
        kernel: Kernel,
        ledger: OperationalLedger,
        pilot: InterfacePilot,
        token_lab_events: TokenLabEventService,
        clock: Callable[[], datetime],
    ) -> None:
        self.kernel = kernel
        self.ledger = ledger
        self.pilot = pilot
        self.token_lab_events = token_lab_events
        self.clock = clock
        self.conversations: dict[str, Conversation] = {}
        self.surface_bindings: dict[str, SurfaceBinding] = {}
        self.pilot_sessions: dict[str, PilotSession] = {}
        self.messages: dict[str, list[ConversationMessage]] = {}
        self.commitments: dict[str, Commitment] = {}
        self.decisions: dict[str, Decision] = {}
        self.node_memories: dict[str, NodeMemory] = {}
        self.action_receipts: dict[str, ConversationActionReceipt] = {}
        self._action_digests: dict[str, str] = {}
        self._creation_receipts: dict[str, ConversationCreatedReceipt] = {}
        self._creation_digests: dict[str, str] = {}
        self._conversation_cursors: dict[str, int] = {}
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
            event_id=deterministic_event_id(
                data_space_id=self.kernel.data_space.data_space_id,
                stream_id=conversation_id,
                idempotency_key=idempotency_key,
            ),
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
        self._conversation_cursors[conversation_id] = result.cursor
        return result.cursor

    @staticmethod
    def _digest(value: object) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def create_conversation(
        self,
        conversation: Conversation,
        surface_binding: SurfaceBinding,
        *,
        idempotency_key: str,
    ) -> ConversationCreatedReceipt:
        request_payload = {
            "conversation": conversation.model_dump(mode="json"),
            "surface_binding": surface_binding.model_dump(mode="json"),
        }
        digest = self._digest(request_payload)
        prior = self._creation_receipts.get(idempotency_key)
        if prior is not None:
            if self._creation_digests[idempotency_key] != digest:
                raise InvariantViolation("conversation idempotency collision")
            return prior
        node = self.kernel.nodes.get(conversation.interface_node_id)
        if node is None or node.kind != "interface":
            raise InvariantViolation("conversation requires an owner Interface Node")
        if conversation.data_space_id != self.kernel.data_space.data_space_id:
            raise InvariantViolation("Conversation DataSpace mismatch")
        if surface_binding.conversation_id != conversation.conversation_id:
            raise InvariantViolation("SurfaceBinding Conversation mismatch")
        if conversation.conversation_id in self.conversations:
            raise InvariantViolation("Conversation already exists")
        if surface_binding.binding_id in self.surface_bindings:
            raise InvariantViolation("SurfaceBinding already exists")
        cursor = self._record(
            conversation_id=conversation.conversation_id,
            event_type="conversation_created",
            payload={**request_payload, "request_digest": digest},
            actor_type="human",
            actor_id=conversation.owner_id,
            idempotency_key=idempotency_key,
        )
        self.conversations[conversation.conversation_id] = conversation
        self.surface_bindings[surface_binding.binding_id] = surface_binding
        self.messages[conversation.conversation_id] = []
        receipt = ConversationCreatedReceipt(
            conversation_id=conversation.conversation_id,
            surface_binding_id=surface_binding.binding_id,
            event_cursor=cursor,
        )
        self._creation_receipts[idempotency_key] = receipt
        self._creation_digests[idempotency_key] = digest
        return receipt

    def perform_owner_action(
        self,
        *,
        conversation_id: str,
        action: OwnerMessageAction,
        device_id: str,
    ) -> ConversationActionReceipt:
        self.kernel.activation.require_active("owner conversation action")
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise InvariantViolation("Conversation not found")
        action_payload = action.model_dump(mode="json")
        action_digest = self._digest(action_payload)
        prior = self.action_receipts.get(action.action_id)
        if prior is not None:
            if self._action_digests[action.action_id] != action_digest:
                raise InvariantViolation("conversation action identity collision")
            return prior
        bindings = [
            binding
            for binding in self.surface_bindings.values()
            if binding.conversation_id == conversation_id
        ]
        if not any(
            binding.device_id == device_id
            and binding.surface == action.source.surface
            and binding.source_session_id == action.source.source_session_id
            and binding.channel_id == action.source.channel_id
            for binding in bindings
        ):
            raise InvariantViolation("owner action does not match an authenticated SurfaceBinding")

        blob_ref = self.ledger.put_blob(action.text.encode("utf-8"))
        owner_message = ConversationMessage(
            message_id=new_id("message"),
            conversation_id=conversation_id,
            role="owner",
            display_text=None,
            blob_ref=blob_ref,
            occurred_at=action.source.occurred_at,
            source=action.source,
        )
        cursor = self._record(
            conversation_id=conversation_id,
            event_type="owner_message_received",
            payload={
                "action_id": action.action_id,
                "action_digest": action_digest,
                "message": owner_message.model_dump(mode="json"),
            },
            actor_type="human",
            actor_id=conversation.owner_id,
            idempotency_key=f"{action.idempotency_key}:owner",
        )
        self.messages[conversation_id].append(owner_message)
        accepted = ConversationActionReceipt(
            action_id=action.action_id,
            conversation_id=conversation_id,
            status="accepted",
            owner_message_id=owner_message.message_id,
            interface_message=None,
            event_cursor=cursor,
            error=None,
        )
        self.action_receipts[action.action_id] = accepted
        self._action_digests[action.action_id] = action_digest

        session = next(
            (
                item
                for item in self.pilot_sessions.values()
                if item.conversation_id == conversation_id
            ),
            None,
        )
        through_cursor = cursor
        if session is None:
            resume_pack = self._resume_pack(conversation)
            provider_session_id = None
            after_cursor = 0
        else:
            resume_pack = None
            provider_session_id = session.provider_session_id
            after_cursor = session.last_event_cursor
        context = InterfaceTurn(
            owner_message_blob_ref=blob_ref,
            event_delta=self._event_delta(
                after_cursor=after_cursor, through_cursor=through_cursor
            ),
            resume_pack=resume_pack,
            provider_session_id=provider_session_id,
        )
        try:
            response = self.pilot.respond(owner_text=action.text, context=context)
            self._validate_actions(conversation_id, response)
            return self._complete_action(
                conversation=conversation,
                action=action,
                action_digest=action_digest,
                owner_message=owner_message,
                response=response,
            )
        except Exception as exc:
            failed_cursor = self._record(
                conversation_id=conversation_id,
                event_type="conversation_action_failed",
                payload={
                    "action_id": action.action_id,
                    "action_digest": action_digest,
                    "owner_message_id": owner_message.message_id,
                    "error": str(exc),
                },
                actor_type="system",
                actor_id=None,
                idempotency_key=f"{action.idempotency_key}:failed",
            )
            failed = accepted.model_copy(
                update={
                    "status": "failed",
                    "event_cursor": failed_cursor,
                    "error": str(exc),
                }
            )
            self.action_receipts[action.action_id] = failed
            return failed

    def _validate_actions(
        self, conversation_id: str, response: StructuredInterfaceResponse
    ) -> None:
        action_ids = [action.action_id for action in response.actions]
        if len(action_ids) != len(set(action_ids)):
            raise InvariantViolation(
                "Interface response action identities must be unique"
            )
        projected_work = dict(self.kernel.work_items)
        created_work_ids = {
            action.work_item.work_item_id
            for action in response.actions
            if isinstance(action, CreateWorkItemAction)
        }
        if created_work_ids & set(projected_work):
            raise InvariantViolation(
                "Interface WorkItem create action reuses an existing identity"
            )
        projected_dependencies = {
            work_id: {
                edge.target_work_item_id
                for edge in self.kernel.work_edges
                if (
                    edge.source_work_item_id == work_id
                    and edge.kind.value == "depends_on"
                )
            }
            for work_id in projected_work
        }
        decision_ids = set(self.decisions)
        decision_ids.update(
            action.action_id
            for action in response.actions
            if isinstance(action, RecordDecisionAction)
        )
        for action in response.actions:
            if isinstance(action, CreateWorkItemAction):
                work = action.work_item
                if (
                    work.data_space_id != self.kernel.data_space.data_space_id
                    or work.state not in (WorkState.PROPOSED, WorkState.READY)
                    or work.completion_evidence is not None
                    or work.blocking_s3_star_finding_ids
                    or not work.acceptance_criteria
                ):
                    raise InvariantViolation(
                        "Interface WorkItem create action violates Kernel contract"
                    )
                if any(
                    node_id not in self.kernel.nodes
                    for node_id in (
                        work.owner_node_id,
                        work.delegated_to_node_id,
                        work.integration_owner_node_id,
                    )
                ):
                    raise InvariantViolation(
                        "Interface WorkItem references an unknown Node"
                    )
                if (
                    work.parent_work_item_id is not None
                    and work.parent_work_item_id not in projected_work
                ):
                    raise InvariantViolation(
                        "Interface WorkItem parent does not exist"
                    )
                if any(
                    dependency_id not in projected_work
                    for dependency_id in action.depends_on_work_item_ids
                ):
                    raise InvariantViolation(
                        "Interface WorkItem dependency does not exist"
                    )
                projected_work[work.work_item_id] = work
                projected_dependencies[work.work_item_id] = set(
                    action.depends_on_work_item_ids
                )
            elif isinstance(action, DelegateWorkItemAction):
                work = projected_work.get(action.work_item_id)
                if (
                    work is None
                    or action.delegated_to_node_id not in self.kernel.nodes
                    or work.state not in (WorkState.PROPOSED, WorkState.READY)
                    or any(
                        execution.work_item_id == action.work_item_id
                        for execution in self.kernel.executions.values()
                    )
                ):
                    raise InvariantViolation(
                        "Interface delegation action violates Kernel contract"
                    )
                projected_work[action.work_item_id] = work.model_copy(
                    update={
                        "delegated_to_node_id": action.delegated_to_node_id,
                        "state": WorkState.READY,
                    }
                )
            elif isinstance(action, RecordDecisionAction):
                if (
                    action.supersedes_decision_id is not None
                    and action.supersedes_decision_id not in decision_ids
                ):
                    raise InvariantViolation(
                        "Interface decision supersedes unknown evidence"
                    )
            elif isinstance(action, UpdateCommitmentAction):
                if (
                    action.work_item_id is not None
                    and action.work_item_id not in projected_work
                ):
                    raise InvariantViolation(
                        "Interface commitment references unknown WorkItem"
                    )
            elif isinstance(action, PlanEffectAction):
                lease = action.effect_lease
                execution = self.kernel.executions.get(lease.execution_id)
                if (
                    lease.data_space_id != self.kernel.data_space.data_space_id
                    or execution is None
                    or execution.work_item_id != lease.work_item_id
                    or lease.expires_at <= self.clock()
                    or lease.state.value != "planned"
                ):
                    raise InvariantViolation(
                        "Interface Effect plan violates lease contract"
                    )
            elif isinstance(action, ProposeCompletionAction):
                work = projected_work.get(action.work_item_id)
                evidence = action.evidence
                if (
                    work is None
                    or work.blocking_s3_star_finding_ids
                    or not evidence.acceptance_satisfied
                    or not evidence.required_tests_passed
                    or evidence.blocking_deviations
                    or not evidence.independent_s3_star_gate
                    or not evidence.integration_branch_merged
                    or not evidence.remote_push_succeeded
                ):
                    raise InvariantViolation(
                        "Interface completion action fails the completion gate"
                    )
            elif isinstance(action, SelectPilotAction):
                work = projected_work.get(action.work_item_id)
                if work is None or work.route_key != action.route_key:
                    raise InvariantViolation(
                        "Interface pilot selection does not match WorkItem route"
                    )
            else:
                raise InvariantViolation(
                    f"InterfaceAction is not valid in a normal owner turn: {action.kind}"
                )

        def visit(work_id: str, path: set[str]) -> None:
            if work_id in path:
                raise InvariantViolation(
                    "Interface WorkItem actions create a dependency cycle"
                )
            for dependency in projected_dependencies.get(work_id, set()):
                visit(dependency, {*path, work_id})

        for work_id in projected_dependencies:
            visit(work_id, set())

    def _complete_action(
        self,
        *,
        conversation: Conversation,
        action: OwnerMessageAction,
        action_digest: str,
        owner_message: ConversationMessage,
        response: StructuredInterfaceResponse,
    ) -> ConversationActionReceipt:
        interface_message = ConversationMessage(
            message_id=new_id("message"),
            conversation_id=conversation.conversation_id,
            role="interface",
            display_text=response.display_text,
            blob_ref=None,
            occurred_at=self.clock(),
            source=None,
        )
        prior_session = next(
            (
                item
                for item in self.pilot_sessions.values()
                if item.conversation_id == conversation.conversation_id
            ),
            None,
        )
        pilot_session_id = (
            prior_session.pilot_session_id
            if prior_session is not None
            else new_id("pilot-session")
        )
        cursor = self._record(
            conversation_id=conversation.conversation_id,
            event_type="interface_response_recorded",
            payload={
                "action_id": action.action_id,
                "action_digest": action_digest,
                "owner_message_id": owner_message.message_id,
                "message": interface_message.model_dump(mode="json"),
                "actions": [
                    item.model_dump(mode="json") for item in response.actions
                ],
                "pilot_session_id": pilot_session_id,
                "root_provider_session_id": (
                    response.provider_session_id
                    if prior_session is None
                    else prior_session.root_provider_session_id
                ),
                "provider_session_id": response.provider_session_id,
                "pilot_usage": response.pilot_usage.model_dump(mode="json"),
                "context_mode": (
                    "resume_pack"
                    if not any(
                        session.conversation_id == conversation.conversation_id
                        for session in self.pilot_sessions.values()
                    )
                    else "provider_delta"
                ),
            },
            actor_type="pilot",
            actor_id=conversation.interface_node_id,
            idempotency_key=f"{action.idempotency_key}:response",
        )
        self.messages[conversation.conversation_id].append(interface_message)
        incidents: set[TokenIncidentKind] = set()
        usage = response.pilot_usage
        if usage.classifier_triggered:
            incidents.add(TokenIncidentKind.PERMISSION_CLASSIFIER)
        if usage.model_substitution:
            incidents.add(TokenIncidentKind.MODEL_SUBSTITUTION)
        if usage.full_history_resent:
            incidents.add(TokenIncidentKind.CONTEXT_RELOAD)
        if usage.polling_call:
            incidents.add(TokenIncidentKind.MODEL_CALL_POLLING)
        if usage.false_complete:
            incidents.add(TokenIncidentKind.FALSE_COMPLETE)
        if usage.reedited_tokens:
            incidents.add(TokenIncidentKind.REEDIT)
        self.token_lab_events.observe(
            TokenObservation(
                observation_id=f"observation:{action.action_id.split(':', 1)[-1]}",
                work_type="interface",
                occurred_at=self.clock(),
                total_input_tokens=usage.input_tokens,
                interface_input_tokens=usage.input_tokens,
                incident_kinds=frozenset(incidents),
                full_history_resent=usage.full_history_resent,
                expensive_interface_calls=1,
                verified_complete=not usage.false_complete,
            ),
            actor_id=conversation.interface_node_id,
            idempotency_key=f"{action.idempotency_key}:token-observation",
        )
        session = prior_session
        if session is None:
            session = PilotSession(
                pilot_session_id=pilot_session_id,
                conversation_id=conversation.conversation_id,
                pilot_id="pilot:interface",
                root_provider_session_id=response.provider_session_id,
                provider_session_id=response.provider_session_id,
                last_event_cursor=cursor,
            )
        else:
            session = session.model_copy(
                update={
                    "provider_session_id": response.provider_session_id,
                    "last_event_cursor": cursor,
                }
            )
        self.pilot_sessions[session.pilot_session_id] = session
        self._apply_actions(conversation.conversation_id, response)
        receipt = ConversationActionReceipt(
            action_id=action.action_id,
            conversation_id=conversation.conversation_id,
            status="completed",
            owner_message_id=owner_message.message_id,
            interface_message=interface_message,
            event_cursor=cursor,
            error=None,
        )
        self.action_receipts[action.action_id] = receipt
        return receipt

    def record_reorientation_session(
        self,
        *,
        conversation_id: str,
        provider_session_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> PilotSession:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise InvariantViolation(
                "reorientation requires an existing canonical Conversation"
            )
        existing = next(
            (
                session
                for session in self.pilot_sessions.values()
                if session.conversation_id == conversation_id
            ),
            None,
        )
        pilot_session_id = (
            new_id("pilot-session")
            if existing is None
            else existing.pilot_session_id
        )
        root_provider_session_id = (
            provider_session_id
            if existing is None
            else existing.root_provider_session_id
        )
        cursor = self._record(
            conversation_id=conversation_id,
            event_type="reorientation_session_advanced",
            payload={
                "pilot_session_id": pilot_session_id,
                "root_provider_session_id": root_provider_session_id,
                "provider_session_id": provider_session_id,
            },
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        session = PilotSession(
            pilot_session_id=pilot_session_id,
            conversation_id=conversation_id,
            pilot_id="pilot:interface",
            root_provider_session_id=root_provider_session_id,
            provider_session_id=provider_session_id,
            last_event_cursor=cursor,
        )
        self.pilot_sessions[pilot_session_id] = session
        return session

    def materialize_history_commitments(
        self,
        *,
        conversation_id: str,
        commitments: tuple[Commitment, ...],
        history_result_event_ids: tuple[str, ...],
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        if conversation_id not in self.conversations:
            raise InvariantViolation("history commitment materialization needs Conversation")
        if len({item.commitment_id for item in commitments}) != len(commitments):
            raise InvariantViolation("history commitment identities must be unique")
        if any(item.conversation_id != conversation_id for item in commitments):
            raise InvariantViolation("history commitment Conversation mismatch")
        self._record(
            conversation_id=conversation_id,
            event_type="history_commitments_materialized",
            payload={
                "commitments": [item.model_dump(mode="json") for item in commitments],
                "history_result_event_ids": list(history_result_event_ids),
            },
            actor_type="system",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        for commitment in commitments:
            existing = self.commitments.get(commitment.commitment_id)
            if existing is not None and existing != commitment:
                raise InvariantViolation("history commitment identity collision")
            self.commitments[commitment.commitment_id] = commitment

    def materialize_reorientation_assessment(
        self,
        *,
        assessment: ReorientationAssessment,
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        conversation = self.conversations.get(assessment.conversation_id)
        if conversation is None:
            raise InvariantViolation("reorientation assessment Conversation not found")
        assessment_blob_ref = self.kernel.ledger.put_blob(
            assessment.model_dump_json().encode("utf-8")
        )
        memory = NodeMemory(
            memory_id=new_id("memory"),
            node_id=conversation.interface_node_id,
            statement=assessment.understanding,
            source_blob_ref=assessment_blob_ref,
        )
        decisions = tuple(
            Decision(
                decision_id=new_id("decision"),
                conversation_id=conversation.conversation_id,
                statement=statement,
                supersedes_decision_id=None,
            )
            for statement in assessment.decisions_and_constraints
        )
        self._record(
            conversation_id=conversation.conversation_id,
            event_type="reorientation_assessment_materialized",
            payload={
                "assessment_id": assessment.assessment_id,
                "memory": memory.model_dump(mode="json"),
                "decisions": [item.model_dump(mode="json") for item in decisions],
            },
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.node_memories[memory.memory_id] = memory
        for decision in decisions:
            self.decisions[decision.decision_id] = decision

    def _resume_pack(self, conversation: Conversation) -> InterfaceResumePack:
        superseded = {
            decision.supersedes_decision_id
            for decision in self.decisions.values()
            if decision.supersedes_decision_id is not None
        }
        return InterfaceResumePack(
            node_memory=tuple(
                memory.model_dump(mode="json")
                for memory in self.node_memories.values()
                if memory.node_id == conversation.interface_node_id
            ),
            unfinished_work_items=tuple(
                work.model_dump(mode="json")
                for work in self.kernel.work_items.values()
                if work.owner_node_id == conversation.interface_node_id
                and work.state not in (WorkState.COMPLETED, WorkState.CANCELLED)
            ),
            open_commitments=tuple(
                commitment.model_dump(mode="json")
                for commitment in self.commitments.values()
                if commitment.conversation_id == conversation.conversation_id
                and commitment.state == "open"
            ),
            active_decisions=tuple(
                decision.model_dump(mode="json")
                for decision in self.decisions.values()
                if decision.conversation_id == conversation.conversation_id
                and decision.decision_id not in superseded
            ),
        )

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

    def _apply_actions(
        self, conversation_id: str, response: StructuredInterfaceResponse
    ) -> None:
        conversation = self.conversations[conversation_id]
        for action in response.actions:
            if isinstance(action, CreateWorkItemAction):
                self.kernel.create_work_item(
                    action.work_item,
                    actor_id=conversation.interface_node_id,
                    idempotency_key=f"interface:{action.action_id}:create",
                )
                for dependency_id in action.depends_on_work_item_ids:
                    self.kernel.add_dependency(
                        work_item_id=action.work_item.work_item_id,
                        depends_on_id=dependency_id,
                        actor_id=conversation.interface_node_id,
                        idempotency_key=(
                            f"interface:{action.action_id}:depends:{dependency_id}"
                        ),
                    )
            elif isinstance(action, DelegateWorkItemAction):
                self.kernel.delegate_work_item(
                    action.work_item_id,
                    delegated_to_node_id=action.delegated_to_node_id,
                    actor_id=conversation.interface_node_id,
                    idempotency_key=f"interface:{action.action_id}:delegate",
                )
            elif isinstance(action, RecordDecisionAction):
                decision = Decision(
                    decision_id=action.action_id,
                    conversation_id=conversation_id,
                    statement=action.statement,
                    supersedes_decision_id=action.supersedes_decision_id,
                )
                self.decisions[decision.decision_id] = decision
            elif isinstance(action, UpdateCommitmentAction):
                commitment = Commitment(
                    commitment_id=action.commitment_id,
                    conversation_id=conversation_id,
                    statement=action.statement,
                    work_item_id=action.work_item_id,
                    state=action.state,
                )
                self.commitments[commitment.commitment_id] = commitment
            elif isinstance(action, PlanEffectAction):
                self.kernel.plan_effect(
                    action.effect_lease,
                    actor_id=conversation.interface_node_id,
                    idempotency_key=f"interface:{action.action_id}:effect",
                )
            elif isinstance(action, ProposeCompletionAction):
                self.kernel.complete_work_item(
                    action.work_item_id,
                    action.evidence,
                    actor_id=conversation.interface_node_id,
                    idempotency_key=f"interface:{action.action_id}:completion",
                )
            elif isinstance(action, SelectPilotAction):
                work = self.kernel.work_items.get(action.work_item_id)
                if work is None or work.route_key != action.route_key:
                    raise InvariantViolation(
                        "pilot selection must match an existing WorkItem route_key"
                    )
            else:
                raise InvariantViolation(
                    f"InterfaceAction is not valid in a normal owner turn: {action.kind}"
                )

    def record_owner_correction(
        self,
        *,
        conversation_id: str,
        statement: str,
        actor_id: str,
        idempotency_key: str,
    ) -> Decision:
        if conversation_id not in self.conversations:
            raise InvariantViolation("Conversation not found")
        decision = Decision(
            decision_id=new_id("decision"),
            conversation_id=conversation_id,
            statement=statement,
            supersedes_decision_id=None,
        )
        self._record(
            conversation_id=conversation_id,
            event_type="owner_correction_recorded",
            payload={"decision": decision.model_dump(mode="json")},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        self.decisions[decision.decision_id] = decision
        return decision

    def action_receipt(
        self, conversation_id: str, action_id: str
    ) -> ConversationActionReceipt:
        receipt = self.action_receipts.get(action_id)
        if receipt is None or receipt.conversation_id != conversation_id:
            raise InvariantViolation("Conversation action receipt not found")
        return receipt

    def status(self, conversation_id: str) -> ConversationStatus:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise InvariantViolation("Conversation not found")
        return ConversationStatus(
            conversation=conversation,
            surface_bindings=tuple(
                binding
                for binding in self.surface_bindings.values()
                if binding.conversation_id == conversation_id
            ),
            pilot_sessions=tuple(
                session
                for session in self.pilot_sessions.values()
                if session.conversation_id == conversation_id
            ),
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
