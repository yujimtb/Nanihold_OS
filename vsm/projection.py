from __future__ import annotations

import hashlib
import json
from datetime import datetime

from vsm.errors import InvariantViolation
from vsm.interface.models import (
    Commitment,
    Conversation,
    ConversationMessage,
    Decision,
    NodeMemory,
)
from vsm.interface.service import InterfaceService
from vsm.kernel.models import (
    BudgetReservation,
    CapabilityGrant,
    CompletionEvidence,
    EffectLease,
    EffectLeaseState,
    Execution,
    ExecutionState,
    ReferenceGrant,
    RouteSnapshot,
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

    def _on_execution_created(self, event) -> None:
        execution = Execution.model_validate(event.payload["execution"])
        self.kernel.executions[execution.execution_id] = execution
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
        self.kernel.route_snapshots[event.stream_id] = snapshot.model_copy(update=update)
        self._kernel_version(event)

    def _on_route_snapshot_published(self, event) -> None:
        snapshot = self.kernel.route_snapshots[event.stream_id]
        self.kernel.route_snapshots[event.stream_id] = snapshot.model_copy(
            update={"state": RouteSnapshotState.PUBLISHED}
        )
        self._kernel_version(event)

    def _on_conversation_created(self, event) -> None:
        conversation = Conversation.model_validate(event.payload["conversation"])
        stored = conversation.model_copy(update={"last_event_cursor": self.cursor + 1})
        self.interface.conversations[conversation.conversation_id] = stored
        self.interface.messages[conversation.conversation_id] = []
        self._interface_version(event)

    def _on_owner_message_received(self, event) -> None:
        message = ConversationMessage.model_validate(event.payload["message"])
        self.interface.messages[message.conversation_id].append(message)
        self._interface_version(event)

    def _on_interface_response_recorded(self, event) -> None:
        message = ConversationMessage.model_validate(event.payload["message"])
        self.interface.messages[message.conversation_id].append(message)
        conversation = self.interface.conversations[message.conversation_id]
        self.interface.conversations[message.conversation_id] = conversation.model_copy(
            update={
                "provider_session_id": event.payload["provider_session_id"],
                "last_event_cursor": self.cursor + 1,
            }
        )
        response = {
            "display_text": message.display_text,
            "work_directives": event.payload["work_directives"],
            "decisions": event.payload["decisions"],
            "commitment_updates": event.payload["commitment_updates"],
            "provider_session_id": event.payload["provider_session_id"],
        }
        from vsm.pilot.models import StructuredInterfaceResponse

        self.interface._apply_structured_updates(
            message.conversation_id,
            StructuredInterfaceResponse.model_validate(response),
        )
        self._interface_version(event)

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
            "capability_grants": self.kernel.capability_grants,
            "effect_leases": self.kernel.effect_leases,
            "reference_grants": self.kernel.reference_grants,
            "budget_reservations": self.kernel.budget_reservations,
            "findings": self.kernel.findings,
            "route_snapshots": self.kernel.route_snapshots,
            "conversations": self.interface.conversations,
            "messages": self.interface.messages,
            "commitments": self.interface.commitments,
            "decisions": self.interface.decisions,
            "node_memories": self.interface.node_memories,
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
