from __future__ import annotations

import hashlib
import json
from datetime import datetime

from vsm.errors import InvariantViolation
from vsm.agent_naming import AgentIdentityRegistration, assignment_from_payload
from vsm.activation.models import (
    ActivationState,
    CurrentWorkGraphSnapshot,
    HistoryImportReceipt,
    ReorientationAssessment,
    ReorientationInterruptionReason,
    ReorientationRevisionReason,
)
from vsm.auth import BootstrapCodeRecord, BrowserSessionRecord
from vsm.interface.models import (
    Commitment,
    Conversation,
    ConversationActionReceipt,
    ConversationCreatedReceipt,
    ConversationMessage,
    Decision,
    NodeMemory,
    PilotSession,
    RecordDecisionAction,
    SurfaceBinding,
    UpdateCommitmentAction,
)
from vsm.interface.service import InterfaceService
from vsm.notifications import AgentNotification
from vsm.kernel.models import (
    BudgetReservation,
    CapabilityGrant,
    CompletionEvidence,
    EffectLease,
    EffectApproval,
    EffectLeaseState,
    Execution,
    ExecutionState,
    ReferenceGrant,
    RouteSnapshot,
    RouteSnapshotRetirementReason,
    RouteSnapshotState,
    S3StarFinding,
    UVSMNode,
    WorkEdge,
    WorkEdgeKind,
    WorkItem,
    WorkState,
)
from vsm.kernel.service import Kernel
from vsm.routing.bayesian import RoutingEvidenceService, VerifiedRouteOutcome
from vsm.token_lab.lab import (
    TokenBaseline,
    TokenLabEventService,
    TokenObservation,
)


LETHE_EXTERNAL_EVENT_STREAM_PREFIXES = {
    "history.message_imported": "history-message:",
    "history.import_completed": "history-import:",
}


class OperationalProjection:
    """Rebuilds all mutable views from the DataSpace Event Ledger."""

    def __init__(
        self,
        *,
        kernel: Kernel,
        interface: InterfaceService,
        routing_evidence: RoutingEvidenceService | None = None,
        token_lab_events: TokenLabEventService | None = None,
    ) -> None:
        self.kernel = kernel
        self.interface = interface
        self.routing_evidence = routing_evidence
        self.token_lab_events = token_lab_events
        self.cursor = 0

    def rebuild(self, *, page_size: int = 500) -> int:
        if page_size <= 0:
            raise InvariantViolation("projection page_size must be positive")
        while True:
            page = self.kernel.ledger.page(self.cursor, page_size)
            if not page:
                return self.cursor
            for stored in page:
                if stored.cursor != self.cursor + 1:
                    raise InvariantViolation("Event Ledger cursor is not contiguous")
                self.apply(stored.event)
                self.cursor = stored.cursor

    def apply(self, event) -> None:
        if event.data_space_id != self.kernel.data_space.data_space_id:
            raise InvariantViolation("projection event crossed its DataSpace")
        external_stream_prefix = LETHE_EXTERNAL_EVENT_STREAM_PREFIXES.get(
            event.event_type
        )
        if external_stream_prefix is not None:
            if not event.stream_id.startswith(external_stream_prefix):
                raise InvariantViolation(
                    "LETHE external event does not match its reserved history stream"
                )
            return
        known_version = max(
            self.kernel._versions.get(event.stream_id, 0),
            self.interface._versions.get(event.stream_id, 0),
            (
                self.routing_evidence._versions.get(event.stream_id, 0)
                if self.routing_evidence is not None
                else 0
            ),
            (
                self.token_lab_events._versions.get(event.stream_id, 0)
                if self.token_lab_events is not None
                else 0
            ),
            (
                self.kernel.activation._version
                if event.stream_id == self.kernel.activation._stream_id
                else 0
            ),
            (
                self.kernel.owner_bootstrap._version
                if event.stream_id == self.kernel.owner_bootstrap._stream_id
                else 0
            ),
        )
        if event.stream_version != known_version + 1:
            raise InvariantViolation(
                f"projection stream gap for {event.stream_id}: "
                f"expected {known_version + 1}, got {event.stream_version}"
            )
        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is None:
            raise InvariantViolation(
                f"projection has no handler for event type: {event.event_type}"
            )
        handler(event)

    def _kernel_version(self, event) -> None:
        self.kernel._versions[event.stream_id] = event.stream_version

    def _interface_version(self, event) -> None:
        self.interface._versions[event.stream_id] = event.stream_version

    def _on_node_registered(self, event) -> None:
        node = UVSMNode.model_validate(event.payload["node"])
        self.kernel.nodes[node.node_id] = node
        self._kernel_version(event)

    def _on_capability_granted(self, event) -> None:
        grant = CapabilityGrant.model_validate(event.payload["grant"])
        self.kernel.capability_grants[grant.grant_id] = grant
        self._kernel_version(event)

    def _on_work_item_created(self, event) -> None:
        work = WorkItem.model_validate(event.payload["work_item"])
        self.kernel.work_items[work.work_item_id] = work
        if work.parent_work_item_id is not None:
            self.kernel.work_edges.extend(
                (
                    WorkEdge(
                        source_work_item_id=work.parent_work_item_id,
                        target_work_item_id=work.work_item_id,
                        kind=WorkEdgeKind.DELEGATED_TO,
                    ),
                    WorkEdge(
                        source_work_item_id=work.work_item_id,
                        target_work_item_id=work.parent_work_item_id,
                        kind=WorkEdgeKind.INTEGRATED_BY,
                    ),
                )
            )
        self._kernel_version(event)

    def _on_work_dependency_added(self, event) -> None:
        self.kernel.work_edges.append(WorkEdge.model_validate(event.payload["edge"]))
        self._kernel_version(event)

    def _on_current_work_graph_imported(self, event) -> None:
        snapshot = CurrentWorkGraphSnapshot.model_validate(event.payload["snapshot"])
        self.kernel.nodes.update({item.node_id: item for item in snapshot.nodes})
        self.kernel.work_items.update(
            {item.work_item_id: item for item in snapshot.work_items}
        )
        self.kernel.work_edges.extend(snapshot.edges)
        self.kernel.activation.work_graph_snapshot_id = snapshot.snapshot_id
        self._kernel_version(event)

    def _on_work_item_delegated(self, event) -> None:
        work = self.kernel.work_items[event.stream_id]
        self.kernel.work_items[event.stream_id] = work.model_copy(
            update={
                "delegated_to_node_id": event.payload["delegated_to_node_id"],
                "state": WorkState.READY,
            }
        )
        self._kernel_version(event)

    def _on_work_item_resume_prepared(self, event) -> None:
        work = self.kernel.work_items[event.stream_id]
        self.kernel.work_items[event.stream_id] = work.model_copy(
            update={"state": WorkState.READY}
        )
        self._kernel_version(event)

    def _on_execution_created(self, event) -> None:
        execution = Execution.model_validate(event.payload["execution"])
        self.kernel.executions[execution.execution_id] = execution
        self._kernel_version(event)

    def _on_agent_notification_delivered(self, event) -> None:
        notification = AgentNotification.model_validate(event.payload["notification"])
        if notification.source_kind.value == "agent_to_agent":
            self.kernel._validate_agent_message_identity(notification)
        existing = self.kernel.agent_notifications.get(notification.notification_id)
        if existing is not None and existing != notification:
            raise InvariantViolation(
                "agent notification identity collision during projection"
            )
        self.kernel.agent_notifications[notification.notification_id] = notification
        self._kernel_version(event)

    def _on_agent_notification_promoted(self, event) -> None:
        notification = self.kernel.agent_notifications.get(event.stream_id)
        if notification is None:
            raise InvariantViolation(
                "agent notification promotion references an unknown notification"
            )
        work_item_id = event.payload.get("work_item_id")
        if not isinstance(work_item_id, str) or work_item_id not in self.kernel.work_items:
            raise InvariantViolation(
                "agent notification promotion references an unknown WorkItem"
            )
        if not notification.requires_work_item:
            raise InvariantViolation(
                "agent notification without promotion condition was promoted"
            )
        if (
            notification.promoted_work_item_id is not None
            and notification.promoted_work_item_id != work_item_id
        ):
            raise InvariantViolation("agent notification promotion identity collision")
        self.kernel.agent_notifications[event.stream_id] = notification.model_copy(
            update={"promoted_work_item_id": work_item_id}
        )
        self._kernel_version(event)

    def _on_agent_name_assigned(self, event) -> None:
        assignment = assignment_from_payload(event.payload)
        existing_assignment = self.kernel.agent_name_assignments.get(
            assignment.assignment_id
        )
        if existing_assignment is not None and existing_assignment != assignment:
            raise InvariantViolation(
                "agent-name assignment identity collision during projection"
            )
        if any(
            existing.agent_name == assignment.agent_name
            and existing.assignment_id != assignment.assignment_id
            for existing in self.kernel.agent_name_assignments.values()
        ):
            raise InvariantViolation(
                "agent-name assignment name collision during projection"
            )
        execution = self.kernel.executions.get(assignment.execution_id)
        if execution is None:
            raise InvariantViolation(
                "agent-name assignment references an unknown Execution"
            )
        if execution.agent_name is not None:
            raise InvariantViolation("Execution already has an agent name")
        if execution.work_item_id != assignment.work_item_id:
            raise InvariantViolation(
                "agent-name assignment WorkItem does not match Execution"
            )
        self.kernel.agent_name_assignments[assignment.assignment_id] = assignment
        self.kernel.executions[assignment.execution_id] = execution.model_copy(
            update={"agent_name": assignment.agent_name}
        )
        self._kernel_version(event)

    def _on_agent_identity_registered(self, event) -> None:
        registration = AgentIdentityRegistration.model_validate(
            event.payload["registration"]
        )
        existing = self.kernel.agent_name_registrations.get(
            registration.registration_id
        )
        if existing is not None and existing != registration:
            raise InvariantViolation(
                "agent identity registration collision during projection"
            )
        if any(
            identity.agent_name == registration.agent_name
            and identity.registration_id != registration.registration_id
            for identity in self.kernel.agent_name_registrations.values()
        ) or self.kernel.agent_name_is_registered(registration.agent_name):
            if existing is None:
                raise InvariantViolation(
                    "agent identity registration name collision during projection"
                )
        if registration.node_id not in self.kernel.nodes:
            raise InvariantViolation(
                "agent identity registration references an unknown Node"
            )
        self.kernel.agent_name_registrations[registration.registration_id] = (
            registration
        )
        self._kernel_version(event)

    def _on_execution_state_changed(self, event) -> None:
        execution = self.kernel.executions[event.stream_id]
        self.kernel.executions[event.stream_id] = execution.model_copy(
            update={
                "state": ExecutionState(event.payload["state"]),
                "pause_reason": event.payload["pause_reason"],
            }
        )
        self._kernel_version(event)

    def _on_budget_reserved(self, event) -> None:
        reservation = BudgetReservation.model_validate(event.payload["reservation"])
        self.kernel.budget_reservations[reservation.reservation_id] = reservation
        self._kernel_version(event)

    def _on_effect_planned(self, event) -> None:
        lease = EffectLease.model_validate(event.payload["effect_lease"])
        self.kernel.effect_leases[lease.lease_id] = lease
        self._kernel_version(event)

    def _on_pilot_execution_receipt_recorded(self, event) -> None:
        execution = self.kernel.executions[event.stream_id]
        if event.payload.get("agent_name") != execution.agent_name:
            raise InvariantViolation(
                "PilotHost receipt agent name does not match Execution"
            )
        self.kernel.executions[event.stream_id] = execution.model_copy(
            update={
                "state": ExecutionState(event.payload["state"]),
                "provider_session_id": event.payload["provider_session_id"],
                "pause_reason": event.payload["pause_reason"],
            }
        )
        self._kernel_version(event)

    def _on_effect_approved(self, event) -> None:
        approval = EffectApproval.model_validate(event.payload["approval"])
        self.kernel.effect_approvals[approval.lease_id] = approval
        self._kernel_version(event)

    def _effect_state(self, event) -> None:
        lease = self.kernel.effect_leases[event.stream_id]
        self.kernel.effect_leases[event.stream_id] = lease.model_copy(
            update={"state": EffectLeaseState(event.payload["state"])}
        )
        self._kernel_version(event)

    _on_effect_activated = _effect_state
    _on_effect_result_unknown = _effect_state
    _on_effect_reconciled = _effect_state

    def _on_human_intervention(self, event) -> None:
        work = self.kernel.work_items[event.stream_id]
        self.kernel.work_items[event.stream_id] = work.model_copy(
            update={"state": WorkState.PAUSED}
        )
        for execution_id, execution in tuple(self.kernel.executions.items()):
            if (
                execution.work_item_id == event.stream_id
                and execution.state is ExecutionState.ACTIVE
            ):
                self.kernel.executions[execution_id] = execution.model_copy(
                    update={
                        "state": ExecutionState.PAUSED,
                        "pause_reason": "human_intervention",
                    }
                )
        for lease_id, lease in tuple(self.kernel.effect_leases.items()):
            if lease.work_item_id == event.stream_id and lease.state in (
                EffectLeaseState.PLANNED,
                EffectLeaseState.ACTIVE,
            ):
                self.kernel.effect_leases[lease_id] = lease.model_copy(
                    update={"state": EffectLeaseState.REVOKED}
                )
        self._kernel_version(event)

    def _on_s3_star_finding_recorded(self, event) -> None:
        finding = S3StarFinding.model_validate(event.payload["finding"])
        self.kernel.findings[finding.finding_id] = finding
        if finding.severity == "severe":
            work = self.kernel.work_items[finding.work_item_id]
            self.kernel.work_items[work.work_item_id] = work.model_copy(
                update={
                    "state": WorkState.BLOCKED,
                    "blocking_s3_star_finding_ids": (
                        *work.blocking_s3_star_finding_ids,
                        finding.finding_id,
                    ),
                }
            )
        self._kernel_version(event)

    def _on_s3_star_risk_accepted(self, event) -> None:
        finding_id = event.payload["finding_id"]
        finding = self.kernel.findings[finding_id]
        self.kernel.findings[finding_id] = finding.model_copy(
            update={"accepted_by_s5": True}
        )
        work = self.kernel.work_items[event.stream_id]
        blockers = tuple(
            item for item in work.blocking_s3_star_finding_ids if item != finding_id
        )
        self.kernel.work_items[event.stream_id] = work.model_copy(
            update={
                "state": WorkState.READY if not blockers else WorkState.BLOCKED,
                "blocking_s3_star_finding_ids": blockers,
            }
        )
        self._kernel_version(event)

    def _on_work_item_completed(self, event) -> None:
        work = self.kernel.work_items[event.stream_id]
        evidence = CompletionEvidence.model_validate(
            event.payload["completion_evidence"]
        )
        self.kernel.work_items[event.stream_id] = work.model_copy(
            update={"state": WorkState.COMPLETED, "completion_evidence": evidence}
        )
        self._kernel_version(event)

    def _on_pilot_host_disconnected(self, event) -> None:
        pilot_host_id = event.payload["pilot_host_id"]
        for execution_id, execution in tuple(self.kernel.executions.items()):
            if (
                execution.pilot_host_id == pilot_host_id
                and execution.state is ExecutionState.ACTIVE
            ):
                self.kernel.executions[execution_id] = execution.model_copy(
                    update={
                        "state": ExecutionState.PAUSED,
                        "pause_reason": "pilot_host_disconnected",
                    }
                )
        self._kernel_version(event)

    def _on_reference_granted(self, event) -> None:
        grant = ReferenceGrant.model_validate(event.payload["reference_grant"])
        self.kernel.reference_grants[grant.grant_id] = grant
        self._kernel_version(event)

    def _on_route_snapshot_registered(self, event) -> None:
        snapshot = RouteSnapshot.model_validate(event.payload["route_snapshot"])
        self.kernel.route_snapshots[snapshot.snapshot_id] = snapshot
        self._kernel_version(event)

    def _on_route_snapshot_approved(self, event) -> None:
        snapshot = self.kernel.route_snapshots[event.stream_id]
        state = RouteSnapshotState(event.payload["state"])
        update = {"state": state}
        if event.payload["approval"] == "s3_star":
            update["s3_star_approval_event_id"] = event.event_id
        else:
            update["owner_approval_event_id"] = event.event_id
        self.kernel.route_snapshots[event.stream_id] = snapshot.model_copy(
            update=update
        )
        self._kernel_version(event)

    def _on_route_snapshot_published(self, event) -> None:
        snapshot = self.kernel.route_snapshots[event.stream_id]
        self.kernel.route_snapshots[event.stream_id] = snapshot.model_copy(
            update={"state": RouteSnapshotState.PUBLISHED}
        )
        self._kernel_version(event)

    def _on_route_snapshot_retired(self, event) -> None:
        if event.actor_type != "human" or event.actor_id is None:
            raise InvariantViolation(
                "RouteSnapshot retirement must be an explicit human event"
            )
        if set(event.payload) != {
            "reason_code",
            "replacement_snapshot_id",
            "state",
        }:
            raise InvariantViolation("RouteSnapshot retirement payload is invalid")
        try:
            reason_code = RouteSnapshotRetirementReason(
                event.payload["reason_code"]
            )
            next_state = RouteSnapshotState(event.payload["state"])
        except (TypeError, ValueError) as exc:
            raise InvariantViolation(
                "RouteSnapshot retirement payload has invalid enum values"
            ) from exc
        if (
            reason_code
            not in (
                RouteSnapshotRetirementReason.SUPERSEDED_BY_APPROVED_SNAPSHOT,
                RouteSnapshotRetirementReason.ROUTE_DECOMMISSIONED,
            )
            or next_state is not RouteSnapshotState.RETIRED
        ):
            raise InvariantViolation("RouteSnapshot retirement transition is invalid")
        snapshot = self.kernel.route_snapshots.get(event.stream_id)
        if snapshot is None or snapshot.state is not RouteSnapshotState.PUBLISHED:
            raise InvariantViolation(
                "RouteSnapshot retirement requires a published source"
            )
        replacement_snapshot_id = event.payload["replacement_snapshot_id"]
        if reason_code is RouteSnapshotRetirementReason.ROUTE_DECOMMISSIONED:
            if replacement_snapshot_id is not None:
                raise InvariantViolation(
                    "decommissioned RouteSnapshot cannot have a replacement"
                )
            self.kernel.route_snapshots[event.stream_id] = snapshot.model_copy(
                update={"state": RouteSnapshotState.RETIRED}
            )
            self._kernel_version(event)
            return
        if not isinstance(replacement_snapshot_id, str):
            raise InvariantViolation(
                "RouteSnapshot retirement replacement identity is invalid"
            )
        if replacement_snapshot_id == snapshot.snapshot_id:
            raise InvariantViolation(
                "RouteSnapshot retirement replacement must differ from source"
            )
        replacement = self.kernel.route_snapshots.get(replacement_snapshot_id)
        if replacement is None:
            raise InvariantViolation(
                "RouteSnapshot retirement replacement does not exist"
            )
        if replacement.route_key != snapshot.route_key:
            raise InvariantViolation(
                "RouteSnapshot retirement replacement route_key mismatch"
            )
        if replacement.state not in (
            RouteSnapshotState.OWNER_APPROVED,
            RouteSnapshotState.PUBLISHED,
        ):
            raise InvariantViolation(
                "RouteSnapshot retirement replacement is not approved"
            )
        self.kernel.route_snapshots[event.stream_id] = snapshot.model_copy(
            update={"state": RouteSnapshotState.RETIRED}
        )
        self._kernel_version(event)

    def _on_conversation_created(self, event) -> None:
        conversation = Conversation.model_validate(event.payload["conversation"])
        binding = SurfaceBinding.model_validate(event.payload["surface_binding"])
        self.interface.conversations[conversation.conversation_id] = conversation
        self.interface.surface_bindings[binding.binding_id] = binding
        self.interface.messages[conversation.conversation_id] = []
        self.interface._conversation_cursors[conversation.conversation_id] = (
            self.cursor + 1
        )
        receipt = ConversationCreatedReceipt(
            conversation_id=conversation.conversation_id,
            surface_binding_id=binding.binding_id,
            event_cursor=self.cursor + 1,
        )
        self.interface._creation_receipts[event.idempotency_key] = receipt
        self.interface._creation_digests[event.idempotency_key] = event.payload[
            "request_digest"
        ]
        self._interface_version(event)

    def _on_legacy_conversation_created(self, event) -> None:
        conversation = Conversation.model_validate(event.payload["conversation"])
        self.interface.conversations[conversation.conversation_id] = conversation
        self.interface.messages[conversation.conversation_id] = []
        self.interface._conversation_cursors[conversation.conversation_id] = (
            self.cursor + 1
        )
        self._interface_version(event)

    def _on_owner_message_received(self, event) -> None:
        message = ConversationMessage.model_validate(event.payload["message"])
        self.interface.messages[message.conversation_id].append(message)
        receipt = ConversationActionReceipt(
            action_id=event.payload["action_id"],
            conversation_id=message.conversation_id,
            status="accepted",
            owner_message_id=message.message_id,
            interface_message=None,
            event_cursor=self.cursor + 1,
            error=None,
        )
        self.interface.action_receipts[receipt.action_id] = receipt
        self.interface._action_digests[receipt.action_id] = event.payload[
            "action_digest"
        ]
        self.interface._conversation_cursors[message.conversation_id] = self.cursor + 1
        self._interface_version(event)

    def _on_interface_response_recorded(self, event) -> None:
        message = ConversationMessage.model_validate(event.payload["message"])
        self.interface.messages[message.conversation_id].append(message)
        existing = next(
            (
                session
                for session in self.interface.pilot_sessions.values()
                if session.conversation_id == message.conversation_id
            ),
            None,
        )
        if existing is None:
            existing = PilotSession(
                pilot_session_id=event.payload["pilot_session_id"],
                conversation_id=message.conversation_id,
                pilot_id="pilot:interface",
                root_provider_session_id=event.payload["root_provider_session_id"],
                provider_session_id=event.payload["provider_session_id"],
                last_event_cursor=self.cursor + 1,
            )
        else:
            existing = existing.model_copy(
                update={
                    "provider_session_id": event.payload["provider_session_id"],
                    "last_event_cursor": self.cursor + 1,
                }
            )
        self.interface.pilot_sessions[existing.pilot_session_id] = existing
        from pydantic import TypeAdapter
        from vsm.interface.models import InterfaceAction

        adapter = TypeAdapter(InterfaceAction)
        for raw in event.payload["actions"]:
            action = adapter.validate_python(raw)
            if isinstance(action, RecordDecisionAction):
                self.interface.decisions[action.action_id] = Decision(
                    decision_id=action.action_id,
                    conversation_id=message.conversation_id,
                    statement=action.statement,
                    supersedes_decision_id=action.supersedes_decision_id,
                )
            elif isinstance(action, UpdateCommitmentAction):
                self.interface.commitments[action.commitment_id] = Commitment(
                    commitment_id=action.commitment_id,
                    conversation_id=message.conversation_id,
                    statement=action.statement,
                    work_item_id=action.work_item_id,
                    state=action.state,
                )
        receipt = ConversationActionReceipt(
            action_id=event.payload["action_id"],
            conversation_id=message.conversation_id,
            status="completed",
            owner_message_id=event.payload["owner_message_id"],
            interface_message=message,
            event_cursor=self.cursor + 1,
            error=None,
        )
        self.interface.action_receipts[receipt.action_id] = receipt
        self.interface._action_digests[receipt.action_id] = event.payload[
            "action_digest"
        ]
        self.interface._conversation_cursors[message.conversation_id] = self.cursor + 1
        self._interface_version(event)

    def _on_reorientation_session_advanced(self, event) -> None:
        existing = next(
            (
                session
                for session in self.interface.pilot_sessions.values()
                if session.conversation_id == event.stream_id
            ),
            None,
        )
        session = PilotSession(
            pilot_session_id=event.payload["pilot_session_id"],
            conversation_id=event.stream_id,
            pilot_id="pilot:interface",
            root_provider_session_id=event.payload["root_provider_session_id"],
            provider_session_id=event.payload["provider_session_id"],
            last_event_cursor=self.cursor + 1,
        )
        if (
            existing is not None
            and existing.pilot_session_id != session.pilot_session_id
        ):
            raise InvariantViolation(
                "reorientation changed canonical PilotSession identity"
            )
        self.interface.pilot_sessions[session.pilot_session_id] = session
        self.interface._conversation_cursors[event.stream_id] = self.cursor + 1
        self._interface_version(event)

    def _on_history_commitments_materialized(self, event) -> None:
        commitments = tuple(
            Commitment.model_validate(item) for item in event.payload["commitments"]
        )
        if len({item.commitment_id for item in commitments}) != len(commitments):
            raise InvariantViolation("history commitment identities must be unique")
        for commitment in commitments:
            existing = self.interface.commitments.get(commitment.commitment_id)
            if existing is not None and existing != commitment:
                raise InvariantViolation("history commitment identity collision")
            self.interface.commitments[commitment.commitment_id] = commitment
        self.interface._conversation_cursors[event.stream_id] = self.cursor + 1
        self._interface_version(event)

    def _on_reorientation_assessment_materialized(self, event) -> None:
        memory = NodeMemory.model_validate(event.payload["memory"])
        decisions = tuple(
            Decision.model_validate(item) for item in event.payload["decisions"]
        )
        self.interface.node_memories[memory.memory_id] = memory
        for decision in decisions:
            self.interface.decisions[decision.decision_id] = decision
        self.interface._conversation_cursors[event.stream_id] = self.cursor + 1
        self._interface_version(event)

    def _on_conversation_action_failed(self, event) -> None:
        action_id = event.payload["action_id"]
        previous = self.interface.action_receipts.get(action_id)
        if previous is None:
            raise InvariantViolation("failed action has no accepted owner message")
        self.interface.action_receipts[action_id] = previous.model_copy(
            update={
                "status": "failed",
                "event_cursor": self.cursor + 1,
                "error": event.payload["error"],
            }
        )
        self.interface._conversation_cursors[event.stream_id] = self.cursor + 1
        self._interface_version(event)

    def _on_owner_correction_recorded(self, event) -> None:
        decision = Decision.model_validate(event.payload["decision"])
        self.interface.decisions[decision.decision_id] = decision
        self.interface._conversation_cursors[event.stream_id] = self.cursor + 1
        self._interface_version(event)

    def _on_history_import_verified(self, event) -> None:
        activation = self.kernel.activation
        receipt = HistoryImportReceipt.model_validate(event.payload["receipt"])
        conversation_id = event.payload.get("reorientation_conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise InvariantViolation(
                "history import event is missing its canonical Conversation"
            )
        activation.import_receipt = receipt
        activation.reorientation_conversation_id = conversation_id
        activation.sessions = {
            session.session_ref: session for session in receipt.sessions
        }
        activation.state = ActivationState.HISTORY_IMPORTED
        activation.reorientation_attempt_in_progress = False
        activation.reorientation_attempt_started_stream_version = None
        activation.pending_reorientation_revision_reason = None
        activation.import_event_cursor = self.cursor + 1
        activation._version = event.stream_version

    def _on_reorientation_started(self, event) -> None:
        self.kernel.activation.state = ActivationState.REORIENTATION_ONLY
        self.kernel.activation.reorientation_error = None
        self.kernel.activation.pending_reorientation_revision_reason = None
        self.kernel.activation.reorientation_attempt_in_progress = True
        self.kernel.activation.reorientation_attempt_started_stream_version = (
            event.stream_version
        )
        self.kernel.activation._version = event.stream_version

    _on_reorientation_retry_started = _on_reorientation_started
    _on_reorientation_revision_retry_started = _on_reorientation_started

    def _on_reorientation_failed(self, event) -> None:
        self.kernel.activation.reorientation_error = event.payload["error_code"]
        self.kernel.activation.pending_reorientation_revision_reason = None
        self.kernel.activation.reorientation_attempt_in_progress = False
        self.kernel.activation.reorientation_attempt_started_stream_version = None
        self.kernel.activation._version = event.stream_version

    def _on_reorientation_attempt_interrupted(self, event) -> None:
        try:
            reason_code = ReorientationInterruptionReason(
                event.payload.get("reason_code")
            )
        except (TypeError, ValueError) as exc:
            raise InvariantViolation(
                "reorientation interruption event has an invalid reason code"
            ) from exc
        self.kernel.activation.reorientation_error = reason_code
        self.kernel.activation.pending_reorientation_revision_reason = None
        self.kernel.activation.reorientation_attempt_in_progress = False
        self.kernel.activation.reorientation_attempt_started_stream_version = None
        self.kernel.activation._version = event.stream_version

    def _on_reorientation_session_checkpointed(self, event) -> None:
        provider_session_id = event.payload.get("provider_session_id")
        if not isinstance(provider_session_id, str) or not provider_session_id:
            raise InvariantViolation(
                "reorientation checkpoint is missing its provider session"
            )
        self.kernel.activation.reorientation_provider_session_id = provider_session_id
        self.kernel.activation._version = event.stream_version

    def _on_history_query_resolved(self, event) -> None:
        self.kernel.activation.history_query_operations.add(event.payload["operation"])
        self.kernel.activation.history_query_event_ids.add(event.event_id)
        self.kernel.activation._version = event.stream_version

    def _on_history_session_index_page_verified(self, event) -> None:
        # The page blob and cursor are audit evidence. Session coverage is verified
        # during the deterministic reorientation preflight, not reconstructed by a
        # lossy projection.
        self.kernel.activation._version = event.stream_version

    def _on_reorientation_pilot_usage_recorded(self, event) -> None:
        activation = self.kernel.activation
        activation.reorientation_pilot_calls += 1
        activation.reorientation_input_tokens += event.payload["input_tokens"]
        activation.reorientation_output_tokens += event.payload["output_tokens"]
        activation._version = event.stream_version

    def _on_reorientation_assessment_accepted(self, event) -> None:
        self.kernel.activation.assessment = ReorientationAssessment.model_validate(
            event.payload["assessment"]
        )
        self.kernel.activation.state = ActivationState.AWAITING_OWNER_CONFIRMATION
        self.kernel.activation.pending_reorientation_revision_reason = None
        self.kernel.activation.reorientation_attempt_in_progress = False
        self.kernel.activation.reorientation_attempt_started_stream_version = None
        self.kernel.activation._version = event.stream_version

    def _on_reorientation_assessment_revision_requested(self, event) -> None:
        activation = self.kernel.activation
        prior_assessment_id = event.payload.get("prior_assessment_id")
        if (
            activation.assessment is None
            or activation.assessment.assessment_id != prior_assessment_id
        ):
            raise InvariantViolation(
                "assessment revision does not reference the projected assessment"
            )
        try:
            reason_code = ReorientationRevisionReason(event.payload.get("reason_code"))
        except (TypeError, ValueError) as exc:
            raise InvariantViolation(
                "assessment revision event has an invalid reason code"
            ) from exc
        if event.payload.get("state") != ActivationState.REORIENTATION_ONLY:
            raise InvariantViolation(
                "assessment revision event has an invalid activation state"
            )
        activation.assessment = None
        activation.reorientation_error = None
        activation.pending_reorientation_revision_reason = reason_code
        activation.reorientation_attempt_in_progress = False
        activation.reorientation_attempt_started_stream_version = None
        activation.state = ActivationState.REORIENTATION_ONLY
        activation._version = event.stream_version

    def _on_activation_approved(self, event) -> None:
        self.kernel.activation.state = ActivationState.ACTIVE
        self.kernel.activation.approved_at = datetime.fromisoformat(
            event.payload["approved_at"]
        )
        self.kernel.activation._version = event.stream_version

    def _on_owner_bootstrap_issued(self, event) -> None:
        record = BootstrapCodeRecord.model_validate(event.payload["record"])
        self.kernel.owner_bootstrap.codes[record.bootstrap_id] = record
        self.kernel.owner_bootstrap._version = event.stream_version

    def _on_owner_bootstrap_exchanged(self, event) -> None:
        bootstrap_id = event.payload["bootstrap_id"]
        record = self.kernel.owner_bootstrap.codes.get(bootstrap_id)
        if record is None:
            raise InvariantViolation("owner bootstrap exchange references unknown code")
        self.kernel.owner_bootstrap.codes[bootstrap_id] = record.model_copy(
            update={"used_at": datetime.fromisoformat(event.payload["used_at"])}
        )
        session = BrowserSessionRecord.model_validate(event.payload["session"])
        self.kernel.owner_bootstrap.sessions[session.session_id] = session
        self.kernel.owner_bootstrap._version = event.stream_version

    def _on_legacy_conversation_message_imported(self, event) -> None:
        message = ConversationMessage.model_validate(event.payload["message"])
        if message.conversation_id not in self.interface.messages:
            raise InvariantViolation("legacy message references unknown Conversation")
        self.interface.messages[message.conversation_id].append(message)
        self._interface_version(event)

    def _on_legacy_decision_imported(self, event) -> None:
        decision = Decision.model_validate(event.payload["decision"])
        self.interface.decisions[decision.decision_id] = decision
        self._interface_version(event)

    def _on_legacy_commitment_imported(self, event) -> None:
        commitment = Commitment.model_validate(event.payload["commitment"])
        self.interface.commitments[commitment.commitment_id] = commitment
        self._interface_version(event)

    def _on_legacy_node_memory_imported(self, event) -> None:
        memory = NodeMemory.model_validate(event.payload["memory"])
        self.interface.node_memories[memory.memory_id] = memory
        self._interface_version(event)

    def _on_model_outcome_verified(self, event) -> None:
        if self.routing_evidence is None:
            raise InvariantViolation(
                "routing evidence projection service is not configured"
            )
        outcome = VerifiedRouteOutcome.model_validate(event.payload["outcome"])
        self.routing_evidence.replay(
            outcome,
            stream_version=event.stream_version,
            cursor=self.cursor + 1,
        )

    def _on_token_baseline_approved(self, event) -> None:
        if self.token_lab_events is None:
            raise InvariantViolation("Token Lab projection service is not configured")
        baseline = TokenBaseline.model_validate(event.payload["baseline"])
        self.token_lab_events.replay_baseline(
            baseline,
            stream_id=event.stream_id,
            stream_version=event.stream_version,
        )

    def _on_token_observation_recorded(self, event) -> None:
        if self.token_lab_events is None:
            raise InvariantViolation("Token Lab projection service is not configured")
        observation = TokenObservation.model_validate(event.payload["observation"])
        self.token_lab_events.replay_observation(
            observation,
            stream_id=event.stream_id,
            stream_version=event.stream_version,
        )

    def _on_token_weekly_review_recorded(self, event) -> None:
        if self.token_lab_events is None:
            raise InvariantViolation("Token Lab projection service is not configured")
        self.token_lab_events.replay_weekly_review(
            datetime.fromisoformat(event.payload["reviewed_at"]),
            stream_id=event.stream_id,
            stream_version=event.stream_version,
        )

    def digest(self) -> str:
        canonical = {
            "nodes": self.kernel.nodes,
            "work_items": self.kernel.work_items,
            "work_edges": self.kernel.work_edges,
            "executions": self.kernel.executions,
            "agent_name_assignments": self.kernel.agent_name_assignments,
            "agent_name_registrations": self.kernel.agent_name_registrations,
            "agent_notifications": self.kernel.agent_notifications,
            "capability_grants": self.kernel.capability_grants,
            "effect_leases": self.kernel.effect_leases,
            "effect_approvals": self.kernel.effect_approvals,
            "reference_grants": self.kernel.reference_grants,
            "budget_reservations": self.kernel.budget_reservations,
            "findings": self.kernel.findings,
            "route_snapshots": self.kernel.route_snapshots,
            "conversations": self.interface.conversations,
            "surface_bindings": self.interface.surface_bindings,
            "pilot_sessions": self.interface.pilot_sessions,
            "messages": self.interface.messages,
            "commitments": self.interface.commitments,
            "decisions": self.interface.decisions,
            "node_memories": self.interface.node_memories,
            "action_receipts": self.interface.action_receipts,
            "activation": self.kernel.activation.status(),
            "owner_bootstrap_codes": self.kernel.owner_bootstrap.codes,
            "owner_browser_sessions": self.kernel.owner_bootstrap.sessions,
            "routing_outcomes": (
                self.routing_evidence.outcomes
                if self.routing_evidence is not None
                else {}
            ),
            "token_baselines": (
                self.token_lab_events.lab.baselines
                if self.token_lab_events is not None
                else {}
            ),
            "token_observations": (
                self.token_lab_events.lab.observations
                if self.token_lab_events is not None
                else []
            ),
            "token_last_weekly_review_at": (
                self.token_lab_events.lab.last_weekly_review_at
                if self.token_lab_events is not None
                else None
            ),
        }
        encoded = json.dumps(
            canonical,
            default=lambda value: value.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
