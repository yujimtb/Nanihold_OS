from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from vsm.errors import InvariantViolation, ReconciliationRequired
from vsm.activation.service import ActivationService
from vsm.activation.models import ActivationState, CurrentWorkGraphSnapshot
from vsm.agent_naming import AgentNameAssignment
from vsm.auth import OwnerBootstrapService
from vsm.ids import deterministic_event_id
from vsm.kernel.ledger import OperationalLedger
from vsm.kernel.models import (
    AuditPolicy,
    BudgetReservation,
    CapabilityGrant,
    CompletionEvidence,
    ControlPolicy,
    DataSpace,
    EffectLease,
    EffectApproval,
    EffectLeaseState,
    EventEnvelope,
    Execution,
    ExecutionState,
    ReferenceGrant,
    RouteSnapshot,
    RouteSnapshotRetirementReason,
    RouteSnapshotState,
    S3StarFinding,
    S3StarSeverity,
    UVSMNode,
    WorkEdge,
    WorkEdgeKind,
    WorkItem,
    WorkState,
)


class Kernel:
    """u-VSM invariants and state transitions over LETHE's Event Ledger."""

    def __init__(
        self,
        *,
        data_space: DataSpace,
        ledger: OperationalLedger,
        audit_policy: AuditPolicy,
        control_policy: ControlPolicy,
        clock: Callable[[], datetime],
    ) -> None:
        if audit_policy.data_space_id != data_space.data_space_id:
            raise InvariantViolation("AuditPolicy DataSpace mismatch")
        if control_policy.data_space_id != data_space.data_space_id:
            raise InvariantViolation("ControlPolicy DataSpace mismatch")
        self.data_space = data_space
        self.ledger = ledger
        self.audit_policy = audit_policy
        self.control_policy = control_policy
        self.clock = clock
        self.activation = ActivationService(
            data_space_id=data_space.data_space_id,
            ledger=ledger,
            clock=clock,
        )
        self.owner_bootstrap = OwnerBootstrapService(
            data_space_id=data_space.data_space_id,
            owner_id=data_space.owner_id,
            ledger=ledger,
            clock=clock,
            activation_state=lambda: self.activation.state,
            owner_node_exists=lambda: any(
                node.owner_id == data_space.owner_id and node.kind == "interface"
                for node in self.nodes.values()
            ),
        )
        self.nodes: dict[str, UVSMNode] = {}
        self.work_items: dict[str, WorkItem] = {}
        self.work_edges: list[WorkEdge] = []
        self.executions: dict[str, Execution] = {}
        self.agent_name_assignments: dict[str, AgentNameAssignment] = {}
        self.capability_grants: dict[str, CapabilityGrant] = {}
        self.effect_leases: dict[str, EffectLease] = {}
        self.effect_approvals: dict[str, EffectApproval] = {}
        self.reference_grants: dict[str, ReferenceGrant] = {}
        self.budget_reservations: dict[str, BudgetReservation] = {}
        self.findings: dict[str, S3StarFinding] = {}
        self.route_snapshots: dict[str, RouteSnapshot] = {}
        self._versions: dict[str, int] = {}

    def _record(
        self,
        *,
        stream_id: str,
        event_type: str,
        payload: dict[str, object],
        actor_type: str,
        actor_id: str | None,
        idempotency_key: str,
        correlation_id: str | None,
        causation_id: str | None = None,
    ) -> EventEnvelope:
        expected = self._versions.get(stream_id, 0)
        event = EventEnvelope(
            event_id=deterministic_event_id(
                data_space_id=self.data_space.data_space_id,
                stream_id=stream_id,
                idempotency_key=idempotency_key,
            ),
            data_space_id=self.data_space.data_space_id,
            stream_id=stream_id,
            stream_version=expected + 1,
            event_type=event_type,
            occurred_at=self.clock(),
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        result = self.ledger.append(event, expected)
        self._versions[stream_id] = result.stream_version
        return event

    @staticmethod
    def _json(model: object) -> dict[str, object]:
        return json.loads(model.model_dump_json())  # type: ignore[attr-defined]

    def register_node(
        self, node: UVSMNode, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        if node.data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("UVSMNode DataSpace mismatch")
        if node.node_id in self.nodes:
            raise InvariantViolation(f"UVSMNode already exists: {node.node_id}")
        if node.parent_node_id is not None:
            parent = self.nodes.get(node.parent_node_id)
            if parent is None:
                raise InvariantViolation(f"parent UVSMNode not found: {node.parent_node_id}")
            if parent.data_space_id != node.data_space_id:
                raise InvariantViolation("a Node Tree cannot cross a DataSpace")
        event = self._record(
            stream_id=node.node_id,
            event_type="node_registered",
            payload={"node": self._json(node)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=node.node_id,
        )
        self.nodes[node.node_id] = node
        return event

    def grant_capability(
        self, grant: CapabilityGrant, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        if grant.data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("CapabilityGrant DataSpace mismatch")
        if grant.state != "active":
            raise InvariantViolation("new CapabilityGrant must be active")
        if not (grant.valid_from <= self.clock() < grant.expires_at):
            raise InvariantViolation("CapabilityGrant is not currently valid")
        if grant.grantor_node_id not in self.nodes or grant.grantee_node_id not in self.nodes:
            raise InvariantViolation("CapabilityGrant nodes must exist")
        event = self._record(
            stream_id=grant.grant_id,
            event_type="capability_granted",
            payload={"grant": self._json(grant)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=grant.grant_id,
        )
        self.capability_grants[grant.grant_id] = grant
        return event

    def create_work_item(
        self, work_item: WorkItem, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        if work_item.data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("WorkItem DataSpace mismatch")
        if work_item.state not in (WorkState.PROPOSED, WorkState.READY):
            raise InvariantViolation(
                "new WorkItem must start proposed or ready"
            )
        if (
            work_item.completion_evidence is not None
            or work_item.blocking_s3_star_finding_ids
            or not work_item.acceptance_criteria
        ):
            raise InvariantViolation(
                "new WorkItem requires acceptance and cannot start completed or blocked"
            )
        for node_id in (
            work_item.owner_node_id,
            work_item.delegated_to_node_id,
            work_item.integration_owner_node_id,
        ):
            if node_id not in self.nodes:
                raise InvariantViolation(f"WorkItem node not found: {node_id}")
        if work_item.parent_work_item_id is not None:
            parent = self.work_items.get(work_item.parent_work_item_id)
            if parent is None:
                raise InvariantViolation(
                    f"parent WorkItem not found: {work_item.parent_work_item_id}"
                )
            if parent.integration_owner_node_id != work_item.integration_owner_node_id:
                raise InvariantViolation(
                    "delegated child must retain its parent's integration owner"
                )
        event = self._record(
            stream_id=work_item.work_item_id,
            event_type="work_item_created",
            payload={"work_item": self._json(work_item)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=work_item.work_item_id,
        )
        self.work_items[work_item.work_item_id] = work_item
        if work_item.parent_work_item_id is not None:
            self.work_edges.extend(
                (
                    WorkEdge(
                        source_work_item_id=work_item.parent_work_item_id,
                        target_work_item_id=work_item.work_item_id,
                        kind=WorkEdgeKind.DELEGATED_TO,
                    ),
                    WorkEdge(
                        source_work_item_id=work_item.work_item_id,
                        target_work_item_id=work_item.parent_work_item_id,
                        kind=WorkEdgeKind.INTEGRATED_BY,
                    ),
                )
            )
        return event

    def add_dependency(
        self,
        *,
        work_item_id: str,
        depends_on_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        if work_item_id not in self.work_items or depends_on_id not in self.work_items:
            raise InvariantViolation("both WorkItems must exist")
        if work_item_id == depends_on_id:
            raise InvariantViolation("a WorkItem cannot depend on itself")
        edge = WorkEdge(
            source_work_item_id=work_item_id,
            target_work_item_id=depends_on_id,
            kind=WorkEdgeKind.DEPENDS_ON,
        )
        if edge in self.work_edges:
            raise InvariantViolation("dependency already exists")
        pending = [depends_on_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == work_item_id:
                raise InvariantViolation("WorkItem dependency would create a cycle")
            if current in visited:
                continue
            visited.add(current)
            pending.extend(
                item.target_work_item_id
                for item in self.work_edges
                if (
                    item.source_work_item_id == current
                    and item.kind is WorkEdgeKind.DEPENDS_ON
                )
            )
        event = self._record(
            stream_id=work_item_id,
            event_type="work_dependency_added",
            payload={"edge": self._json(edge)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=work_item_id,
        )
        self.work_edges.append(edge)
        return event

    def import_current_work_graph(
        self,
        snapshot: CurrentWorkGraphSnapshot,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        if self.activation.work_graph_snapshot_id is not None:
            if self.activation.work_graph_snapshot_id == snapshot.snapshot_id:
                events = self.ledger.stream(snapshot.snapshot_id, 0, 1)
                if not events:
                    raise InvariantViolation(
                        "imported Work Graph event cannot be reconciled"
                    )
                stored = CurrentWorkGraphSnapshot.model_validate(
                    events[0].event.payload["snapshot"]
                )
                if stored != snapshot:
                    raise InvariantViolation(
                        "Work Graph snapshot identity collision"
                    )
                return events[0].event
            raise InvariantViolation("current Work Graph was already imported")
        if self.activation.state is not ActivationState.UNCOMMISSIONED:
            raise InvariantViolation(
                "current Work Graph import requires uncommissioned state"
            )
        if snapshot.data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("Work Graph snapshot DataSpace mismatch")
        if snapshot.calculated_sha256() != snapshot.snapshot_sha256:
            raise InvariantViolation("Work Graph snapshot digest mismatch")
        nodes = {item.node_id: item for item in snapshot.nodes}
        nodes.update(self.nodes)
        if any(
            node.data_space_id != self.data_space.data_space_id
            for node in snapshot.nodes
        ):
            raise InvariantViolation("Work Graph snapshot Node DataSpace mismatch")
        if any(
            node_id in self.nodes and self.nodes[node_id] != node
            for node_id, node in {
                item.node_id: item for item in snapshot.nodes
            }.items()
        ):
            raise InvariantViolation("Work Graph snapshot Node identity collision")
        work_items = {item.work_item_id: item for item in snapshot.work_items}
        if set(work_items) & set(self.work_items):
            raise InvariantViolation("Work Graph snapshot WorkItem identity collision")
        for work in snapshot.work_items:
            if work.data_space_id != self.data_space.data_space_id:
                raise InvariantViolation(
                    "Work Graph snapshot WorkItem DataSpace mismatch"
                )
            if any(
                node_id not in nodes
                for node_id in (
                    work.owner_node_id,
                    work.delegated_to_node_id,
                    work.integration_owner_node_id,
                )
            ):
                raise InvariantViolation(
                    "Work Graph snapshot WorkItem references an unknown Node"
                )
            if (
                work.parent_work_item_id is not None
                and work.parent_work_item_id not in work_items
            ):
                raise InvariantViolation(
                    "Work Graph snapshot WorkItem parent is missing"
                )
        if any(
            edge.source_work_item_id not in work_items
            or edge.target_work_item_id not in work_items
            for edge in snapshot.edges
        ):
            raise InvariantViolation(
                "Work Graph snapshot edge references an unknown WorkItem"
            )
        event = self._record(
            stream_id=snapshot.snapshot_id,
            event_type="current_work_graph_imported",
            payload={
                "snapshot": snapshot.model_dump(mode="json"),
            },
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=snapshot.snapshot_id,
        )
        self.nodes.update(
            {item.node_id: item for item in snapshot.nodes}
        )
        self.work_items.update(work_items)
        self.work_edges.extend(snapshot.edges)
        self.activation.work_graph_snapshot_id = snapshot.snapshot_id
        return event

    def delegate_work_item(
        self,
        work_item_id: str,
        *,
        delegated_to_node_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        work = self.work_items.get(work_item_id)
        if work is None:
            raise InvariantViolation("WorkItem not found")
        if delegated_to_node_id not in self.nodes:
            raise InvariantViolation("delegated UVSMNode not found")
        if work.state not in (WorkState.PROPOSED, WorkState.READY):
            raise InvariantViolation("only proposed or ready WorkItem can be delegated")
        if any(execution.work_item_id == work_item_id for execution in self.executions.values()):
            raise InvariantViolation("WorkItem with Executions cannot be redelegated")
        updated = work.model_copy(
            update={
                "delegated_to_node_id": delegated_to_node_id,
                "state": WorkState.READY,
            }
        )
        event = self._record(
            stream_id=work_item_id,
            event_type="work_item_delegated",
            payload={"delegated_to_node_id": delegated_to_node_id},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=work_item_id,
        )
        self.work_items[work_item_id] = updated
        return event

    def prepare_owner_confirmed_resume(
        self,
        work_item_id: str,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        work = self.validate_owner_confirmed_resume(work_item_id)
        event = self._record(
            stream_id=work_item_id,
            event_type="work_item_resume_prepared",
            payload={"state": WorkState.READY},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=work_item_id,
        )
        self.work_items[work_item_id] = work.model_copy(
            update={"state": WorkState.READY}
        )
        return event

    def validate_owner_confirmed_resume(self, work_item_id: str) -> WorkItem:
        """Validate a proposed resume without appending an Event or mutating state."""
        if self.activation.state.value != "AWAITING_OWNER_CONFIRMATION":
            raise InvariantViolation(
                "resume preparation requires awaiting owner confirmation"
            )
        assessment = self.activation.assessment
        if (
            assessment is None
            or work_item_id not in assessment.resume_work_item_ids
        ):
            raise InvariantViolation(
                "only assessed resume WorkItems may be prepared"
            )
        work = self.work_items.get(work_item_id)
        if work is None:
            raise InvariantViolation("resume WorkItem not found")
        if work.blocking_s3_star_finding_ids:
            raise InvariantViolation(
                "blocking S3* finding prevents WorkItem resume"
            )
        if work.state not in (
            WorkState.PROPOSED,
            WorkState.READY,
            WorkState.PAUSED,
        ):
            raise InvariantViolation(
                "WorkItem state cannot be resumed after owner confirmation"
            )
        if work.delegated_to_node_id not in self.nodes:
            raise InvariantViolation("resume WorkItem delegated Node not found")
        return work

    def create_execution(
        self, execution: Execution, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        self.activation.require_active("Execution creation")
        if execution.data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("Execution DataSpace mismatch")
        if execution.state not in (
            ExecutionState.REQUESTED,
            ExecutionState.ACTIVE,
        ):
            raise InvariantViolation(
                "new Execution must start requested or active"
            )
        if execution.node_id not in self.nodes:
            raise InvariantViolation("Execution Node not found")
        work = self.work_items.get(execution.work_item_id)
        if work is None:
            raise InvariantViolation("Execution WorkItem not found")
        if execution.node_id != work.delegated_to_node_id:
            raise InvariantViolation("Execution must stay with the delegated Node")
        if execution.execution_id in self.executions:
            raise InvariantViolation("Execution already exists")
        event = self._record(
            stream_id=execution.execution_id,
            event_type="execution_created",
            payload={"execution": self._json(execution)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=execution.work_item_id,
        )
        self.executions[execution.execution_id] = execution
        return event

    def record_agent_name_assignment(
        self,
        assignment: AgentNameAssignment,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        if assignment.data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("AgentNameAssignment DataSpace mismatch")
        if assignment.assignment_id in self.agent_name_assignments:
            raise InvariantViolation(
                f"AgentNameAssignment already exists: {assignment.assignment_id}"
            )
        if any(
            existing.agent_name == assignment.agent_name
            for existing in self.agent_name_assignments.values()
        ):
            raise InvariantViolation(
                f"AgentNameAssignment name is already assigned: {assignment.agent_name}"
            )
        execution = self.executions.get(assignment.execution_id)
        if execution is None:
            raise InvariantViolation("AgentNameAssignment Execution not found")
        if execution.work_item_id != assignment.work_item_id:
            raise InvariantViolation(
                "AgentNameAssignment WorkItem does not match Execution"
            )
        if execution.node_id != assignment.node_id:
            raise InvariantViolation(
                "AgentNameAssignment Node does not match Execution"
            )
        if execution.pilot_id != assignment.pilot_id:
            raise InvariantViolation(
                "AgentNameAssignment Pilot does not match Execution"
            )
        if execution.agent_name is not None:
            raise InvariantViolation("Execution already has an agent name")
        event = self._record(
            stream_id=assignment.assignment_id,
            event_type="agent_name_assigned",
            payload={"assignment": self._json(assignment)},
            actor_type="system",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=assignment.work_item_id,
            causation_id=assignment.execution_id,
        )
        self.agent_name_assignments[assignment.assignment_id] = assignment
        self.executions[assignment.execution_id] = execution.model_copy(
            update={"agent_name": assignment.agent_name}
        )
        return event

    def set_execution_state(
        self,
        execution_id: str,
        state: ExecutionState,
        *,
        actor_type: str,
        actor_id: str,
        idempotency_key: str,
        pause_reason: str | None,
    ) -> EventEnvelope:
        execution = self.executions.get(execution_id)
        if execution is None:
            raise InvariantViolation("Execution not found")
        allowed = {
            ExecutionState.REQUESTED: {
                ExecutionState.ACTIVE,
                ExecutionState.PAUSED,
                ExecutionState.FAILED,
                ExecutionState.CANCELLED,
            },
            ExecutionState.ACTIVE: {
                ExecutionState.PAUSED,
                ExecutionState.SUCCEEDED,
                ExecutionState.FAILED,
                ExecutionState.CANCELLED,
            },
            ExecutionState.PAUSED: {
                ExecutionState.ACTIVE,
                ExecutionState.FAILED,
                ExecutionState.CANCELLED,
            },
            ExecutionState.SUCCEEDED: set(),
            ExecutionState.FAILED: set(),
            ExecutionState.CANCELLED: set(),
        }
        if state not in allowed[execution.state]:
            raise InvariantViolation(
                f"invalid Execution transition: {execution.state} -> {state}"
            )
        if state is ExecutionState.PAUSED and not pause_reason:
            raise InvariantViolation("paused Execution requires a reason")
        updated = execution.model_copy(
            update={"state": state, "pause_reason": pause_reason}
        )
        event = self._record(
            stream_id=execution_id,
            event_type="execution_state_changed",
            payload={"state": state, "pause_reason": pause_reason},
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=execution.work_item_id,
        )
        self.executions[execution_id] = updated
        return event

    def record_pilot_execution_receipt(
        self,
        execution_id: str,
        *,
        receipt_id: str,
        receipt_status: str,
        requested_model: str,
        actual_model: str | None,
        provider_session_id: str | None,
        usage: dict[str, object] | None,
        result: dict[str, object] | None,
        error: dict[str, str] | None,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        execution = self.executions.get(execution_id)
        if execution is None or execution.state is not ExecutionState.REQUESTED:
            raise InvariantViolation(
                "PilotHost receipt requires a requested Execution"
            )
        if receipt_status == "succeeded":
            if (
                actual_model is None
                or provider_session_id is None
                or usage is None
                or result is None
                or error is not None
            ):
                raise InvariantViolation("succeeded PilotHost receipt is incomplete")
            next_state = ExecutionState.SUCCEEDED
            pause_reason = None
        elif receipt_status == "failed":
            if result is not None or error is None:
                raise InvariantViolation("failed PilotHost receipt is inconsistent")
            next_state = ExecutionState.FAILED
            pause_reason = None
        elif receipt_status == "transport_unknown":
            if result is not None or error is None:
                raise InvariantViolation(
                    "transport_unknown PilotHost receipt is inconsistent"
                )
            next_state = ExecutionState.PAUSED
            pause_reason = "pilot_receipt_reconciliation_required"
        else:
            raise InvariantViolation("PilotHost receipt must be terminal")
        event = self._record(
            stream_id=execution_id,
            event_type="pilot_execution_receipt_recorded",
            payload={
                "receipt_id": receipt_id,
                "receipt_status": receipt_status,
                "requested_model": requested_model,
                "actual_model": actual_model,
                "provider_session_id": provider_session_id,
                "usage": usage,
                "result": result,
                "error": error,
                "state": next_state,
                "pause_reason": pause_reason,
                "agent_name": execution.agent_name,
            },
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=execution.work_item_id,
        )
        self.executions[execution_id] = execution.model_copy(
            update={
                "state": next_state,
                "provider_session_id": provider_session_id,
                "pause_reason": pause_reason,
            }
        )
        return event

    def reserve_budget(
        self,
        reservation: BudgetReservation,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        if reservation.data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("BudgetReservation DataSpace mismatch")
        if reservation.work_item_id not in self.work_items:
            raise InvariantViolation("BudgetReservation WorkItem not found")
        event = self._record(
            stream_id=reservation.reservation_id,
            event_type="budget_reserved",
            payload={"reservation": self._json(reservation)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=reservation.work_item_id,
        )
        self.budget_reservations[reservation.reservation_id] = reservation
        return event

    def plan_effect(
        self, lease: EffectLease, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        self.activation.require_active("Effect planning")
        if lease.data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("EffectLease DataSpace mismatch")
        if lease.state is not EffectLeaseState.PLANNED:
            raise InvariantViolation("a new EffectLease must be planned")
        if lease.expires_at <= self.clock():
            raise InvariantViolation("EffectLease must expire in the future")
        execution = self.executions.get(lease.execution_id)
        if execution is None or execution.work_item_id != lease.work_item_id:
            raise InvariantViolation("EffectLease Execution/WorkItem mismatch")
        if any(
            existing.idempotency_key == lease.idempotency_key
            and existing.lease_id != lease.lease_id
            for existing in self.effect_leases.values()
        ):
            raise InvariantViolation("effect idempotency key already belongs to another lease")
        event = self._record(
            stream_id=lease.lease_id,
            event_type="effect_planned",
            payload={"effect_lease": self._json(lease)},
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=lease.work_item_id,
        )
        self.effect_leases[lease.lease_id] = lease
        return event

    def approve_effect(
        self, lease_id: str, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        self.activation.require_active("Effect approval")
        lease = self.effect_leases.get(lease_id)
        if lease is None or lease.state is not EffectLeaseState.PLANNED:
            raise InvariantViolation("only a planned EffectLease can be approved")
        if lease_id in self.effect_approvals:
            raise InvariantViolation("EffectLease is already approved")
        approval = EffectApproval(
            lease_id=lease_id,
            approved_by=actor_id,
            approved_at=self.clock(),
        )
        event = self._record(
            stream_id=lease_id,
            event_type="effect_approved",
            payload={"approval": self._json(approval)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=lease.work_item_id,
        )
        self.effect_approvals[lease_id] = approval
        return event

    def activate_effect(
        self, lease_id: str, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        self.activation.require_active("Effect activation")
        lease = self.effect_leases.get(lease_id)
        if lease is None or lease.state is not EffectLeaseState.PLANNED:
            raise InvariantViolation("only a LETHE-confirmed planned effect can activate")
        if lease.expires_at <= self.clock():
            raise InvariantViolation("expired EffectLease cannot activate")
        if lease_id not in self.effect_approvals:
            raise InvariantViolation("EffectLease requires explicit owner approval")
        event = self._record(
            stream_id=lease_id,
            event_type="effect_activated",
            payload={"state": EffectLeaseState.ACTIVE},
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=lease.work_item_id,
        )
        self.effect_leases[lease_id] = lease.model_copy(
            update={"state": EffectLeaseState.ACTIVE}
        )
        return event

    def mark_effect_unknown(
        self, lease_id: str, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        lease = self.effect_leases.get(lease_id)
        if lease is None or lease.state is not EffectLeaseState.ACTIVE:
            raise InvariantViolation("only an active effect can become unknown")
        event = self._record(
            stream_id=lease_id,
            event_type="effect_result_unknown",
            payload={
                "state": EffectLeaseState.UNKNOWN,
                "effect_idempotency_key": lease.idempotency_key,
            },
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=lease.work_item_id,
        )
        self.effect_leases[lease_id] = lease.model_copy(
            update={"state": EffectLeaseState.UNKNOWN}
        )
        return event

    def require_effect_reconciliation(self, lease_id: str) -> None:
        lease = self.effect_leases.get(lease_id)
        if lease is None:
            raise InvariantViolation("EffectLease not found")
        if lease.state is EffectLeaseState.UNKNOWN:
            raise ReconciliationRequired(
                f"reconcile effect by idempotency key {lease.idempotency_key}"
            )

    def reconcile_effect(
        self,
        lease_id: str,
        result: EffectLeaseState,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        lease = self.effect_leases.get(lease_id)
        if lease is None or lease.state is not EffectLeaseState.UNKNOWN:
            raise InvariantViolation("only an unknown effect can be reconciled")
        if result not in (EffectLeaseState.SUCCEEDED, EffectLeaseState.FAILED):
            raise InvariantViolation("reconciliation result must be succeeded or failed")
        event = self._record(
            stream_id=lease_id,
            event_type="effect_reconciled",
            payload={
                "state": result,
                "effect_idempotency_key": lease.idempotency_key,
            },
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=lease.work_item_id,
        )
        self.effect_leases[lease_id] = lease.model_copy(update={"state": result})
        return event

    def intervene(
        self,
        work_item_id: str,
        *,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        work = self.work_items.get(work_item_id)
        if work is None:
            raise InvariantViolation("WorkItem not found")
        event = self._record(
            stream_id=work_item_id,
            event_type="human_intervention",
            payload={"reason": reason},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=work_item_id,
        )
        self.work_items[work_item_id] = work.model_copy(update={"state": WorkState.PAUSED})
        for execution_id, execution in tuple(self.executions.items()):
            if (
                execution.work_item_id == work_item_id
                and execution.state is ExecutionState.ACTIVE
            ):
                self.executions[execution_id] = execution.model_copy(
                    update={
                        "state": ExecutionState.PAUSED,
                        "pause_reason": "human_intervention",
                    }
                )
        for lease_id, lease in tuple(self.effect_leases.items()):
            if lease.work_item_id == work_item_id and lease.state in (
                EffectLeaseState.PLANNED,
                EffectLeaseState.ACTIVE,
            ):
                self.effect_leases[lease_id] = lease.model_copy(
                    update={"state": EffectLeaseState.REVOKED}
                )
        return event

    def record_s3_star_finding(
        self, finding: S3StarFinding, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        work = self.work_items.get(finding.work_item_id)
        if work is None or finding.node_id not in self.nodes:
            raise InvariantViolation("S3* finding target does not exist")
        if finding.accepted_by_s5:
            raise InvariantViolation("a new S3* finding cannot start accepted")
        event = self._record(
            stream_id=finding.work_item_id,
            event_type="s3_star_finding_recorded",
            payload={"finding": self._json(finding)},
            actor_type="system",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=finding.work_item_id,
        )
        self.findings[finding.finding_id] = finding
        if finding.severity is S3StarSeverity.SEVERE:
            blockers = (*work.blocking_s3_star_finding_ids, finding.finding_id)
            self.work_items[work.work_item_id] = work.model_copy(
                update={
                    "state": WorkState.BLOCKED,
                    "blocking_s3_star_finding_ids": blockers,
                }
            )
        return event

    def accept_s3_star_risk(
        self,
        finding_id: str,
        *,
        s5_node_id: str,
        actor_id: str,
        rationale: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        finding = self.findings.get(finding_id)
        if finding is None:
            raise InvariantViolation("S3* finding not found")
        if s5_node_id != finding.node_id:
            raise InvariantViolation("risk acceptance must come from same-level S5")
        event = self._record(
            stream_id=finding.work_item_id,
            event_type="s3_star_risk_accepted",
            payload={
                "finding_id": finding_id,
                "s5_node_id": s5_node_id,
                "rationale": rationale,
            },
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=finding.work_item_id,
        )
        self.findings[finding_id] = finding.model_copy(update={"accepted_by_s5": True})
        work = self.work_items[finding.work_item_id]
        blockers = tuple(
            blocker
            for blocker in work.blocking_s3_star_finding_ids
            if blocker != finding_id
        )
        self.work_items[work.work_item_id] = work.model_copy(
            update={
                "state": WorkState.READY if not blockers else WorkState.BLOCKED,
                "blocking_s3_star_finding_ids": blockers,
            }
        )
        return event

    def complete_work_item(
        self,
        work_item_id: str,
        evidence: CompletionEvidence,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        work = self.work_items.get(work_item_id)
        if work is None:
            raise InvariantViolation("WorkItem not found")
        if work.blocking_s3_star_finding_ids:
            raise InvariantViolation("blocking S3* findings prevent integration")
        if not all(
            (
                evidence.acceptance_satisfied,
                evidence.required_tests_passed,
                not evidence.blocking_deviations,
                evidence.independent_s3_star_gate,
                evidence.integration_branch_merged,
                evidence.remote_push_succeeded,
            )
        ):
            raise InvariantViolation("completion gate is not satisfied")
        incomplete_dependencies = [
            edge.target_work_item_id
            for edge in self.work_edges
            if edge.source_work_item_id == work_item_id
            and edge.kind is WorkEdgeKind.DEPENDS_ON
            and self.work_items[edge.target_work_item_id].state is not WorkState.COMPLETED
        ]
        if incomplete_dependencies:
            raise InvariantViolation(
                f"incomplete dependencies: {', '.join(incomplete_dependencies)}"
            )
        event = self._record(
            stream_id=work_item_id,
            event_type="work_item_completed",
            payload={"completion_evidence": self._json(evidence)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=work_item_id,
        )
        self.work_items[work_item_id] = work.model_copy(
            update={
                "state": WorkState.COMPLETED,
                "completion_evidence": evidence,
            }
        )
        return event

    def pilot_host_disconnected(
        self, pilot_host_id: str, *, idempotency_key: str
    ) -> EventEnvelope:
        event = self._record(
            stream_id=pilot_host_id,
            event_type="pilot_host_disconnected",
            payload={"pilot_host_id": pilot_host_id},
            actor_type="system",
            actor_id=None,
            idempotency_key=idempotency_key,
            correlation_id=pilot_host_id,
        )
        for execution_id, execution in tuple(self.executions.items()):
            if (
                execution.pilot_host_id == pilot_host_id
                and execution.state is ExecutionState.ACTIVE
            ):
                self.executions[execution_id] = execution.model_copy(
                    update={
                        "state": ExecutionState.PAUSED,
                        "pause_reason": "pilot_host_disconnected",
                    }
                )
        return event

    def register_reference_grant(
        self, grant: ReferenceGrant, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        if grant.target_data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("ReferenceGrant target DataSpace mismatch")
        if grant.state != "active":
            raise InvariantViolation("new ReferenceGrant must be active")
        if not (grant.valid_from <= self.clock() < grant.expires_at):
            raise InvariantViolation("ReferenceGrant is not currently valid")
        event = self._record(
            stream_id=grant.grant_id,
            event_type="reference_granted",
            payload={"reference_grant": self._json(grant)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=grant.grant_id,
        )
        self.reference_grants[grant.grant_id] = grant
        return event

    def register_route_snapshot(
        self, snapshot: RouteSnapshot, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        if snapshot.data_space_id != self.data_space.data_space_id:
            raise InvariantViolation("RouteSnapshot DataSpace mismatch")
        if snapshot.state is not RouteSnapshotState.DRAFT:
            raise InvariantViolation("RouteSnapshot must start as draft")
        event = self._record(
            stream_id=snapshot.snapshot_id,
            event_type="route_snapshot_registered",
            payload={"route_snapshot": self._json(snapshot)},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=snapshot.snapshot_id,
        )
        self.route_snapshots[snapshot.snapshot_id] = snapshot
        return event

    def approve_route_snapshot(
        self,
        snapshot_id: str,
        *,
        approval: str,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        snapshot = self.route_snapshots.get(snapshot_id)
        if snapshot is None:
            raise InvariantViolation("RouteSnapshot not found")
        if approval == "s3_star" and snapshot.state is RouteSnapshotState.DRAFT:
            next_state = RouteSnapshotState.S3_STAR_APPROVED
            field = "s3_star_approval_event_id"
        elif (
            approval == "owner"
            and snapshot.state is RouteSnapshotState.S3_STAR_APPROVED
        ):
            next_state = RouteSnapshotState.OWNER_APPROVED
            field = "owner_approval_event_id"
        else:
            raise InvariantViolation("RouteSnapshot approvals are ordered S3* then owner")
        event = self._record(
            stream_id=snapshot_id,
            event_type="route_snapshot_approved",
            payload={"approval": approval, "state": next_state},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=snapshot_id,
        )
        self.route_snapshots[snapshot_id] = snapshot.model_copy(
            update={"state": next_state, field: event.event_id}
        )
        return event

    def publish_route_snapshot(
        self, snapshot_id: str, *, actor_id: str, idempotency_key: str
    ) -> EventEnvelope:
        snapshot = self.route_snapshots.get(snapshot_id)
        if snapshot is None or snapshot.state is not RouteSnapshotState.OWNER_APPROVED:
            raise InvariantViolation("only an owner-approved RouteSnapshot can publish")
        if any(
            other.snapshot_id != snapshot_id
            and other.route_key == snapshot.route_key
            and other.state is RouteSnapshotState.PUBLISHED
            for other in self.route_snapshots.values()
        ):
            raise InvariantViolation(
                "retire the published RouteSnapshot for this route_key "
                "before publishing"
            )
        event = self._record(
            stream_id=snapshot_id,
            event_type="route_snapshot_published",
            payload={"state": RouteSnapshotState.PUBLISHED},
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=snapshot_id,
        )
        self.route_snapshots[snapshot_id] = snapshot.model_copy(
            update={"state": RouteSnapshotState.PUBLISHED}
        )
        return event

    def retire_route_snapshot(
        self,
        snapshot_id: str,
        *,
        reason_code: RouteSnapshotRetirementReason,
        replacement_snapshot_id: str | None,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        valid_reason = isinstance(
            reason_code, RouteSnapshotRetirementReason
        ) and reason_code in (
            RouteSnapshotRetirementReason.SUPERSEDED_BY_APPROVED_SNAPSHOT,
            RouteSnapshotRetirementReason.ROUTE_DECOMMISSIONED,
        )
        if not valid_reason:
            raise InvariantViolation("unsupported RouteSnapshot retirement reason")
        snapshot = self.route_snapshots.get(snapshot_id)
        if snapshot is None:
            raise InvariantViolation("RouteSnapshot not found")
        if snapshot.state is not RouteSnapshotState.PUBLISHED:
            raise InvariantViolation("only a published RouteSnapshot can retire")
        if reason_code is RouteSnapshotRetirementReason.ROUTE_DECOMMISSIONED:
            if replacement_snapshot_id is not None:
                raise InvariantViolation(
                    "decommissioned route must not have a replacement RouteSnapshot"
                )
        elif replacement_snapshot_id is None:
            raise InvariantViolation(
                "superseded RouteSnapshot requires a replacement RouteSnapshot"
            )
        else:
            self._validate_route_snapshot_replacement(
                snapshot=snapshot,
                replacement_snapshot_id=replacement_snapshot_id,
            )
        event = self._record(
            stream_id=snapshot_id,
            event_type="route_snapshot_retired",
            payload={
                "reason_code": reason_code,
                "replacement_snapshot_id": replacement_snapshot_id,
                "state": RouteSnapshotState.RETIRED,
            },
            actor_type="human",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            correlation_id=snapshot_id,
        )
        self.route_snapshots[snapshot_id] = snapshot.model_copy(
            update={"state": RouteSnapshotState.RETIRED}
        )
        return event

    def _validate_route_snapshot_replacement(
        self,
        *,
        snapshot: RouteSnapshot,
        replacement_snapshot_id: str,
    ) -> None:
        if replacement_snapshot_id == snapshot.snapshot_id:
            raise InvariantViolation(
                "replacement RouteSnapshot must differ from retired RouteSnapshot"
            )
        replacement = self.route_snapshots.get(replacement_snapshot_id)
        if replacement is None:
            raise InvariantViolation("replacement RouteSnapshot not found")
        if replacement.route_key != snapshot.route_key:
            raise InvariantViolation(
                "replacement RouteSnapshot must have the same route_key"
            )
        if replacement.state not in (
            RouteSnapshotState.OWNER_APPROVED,
            RouteSnapshotState.PUBLISHED,
        ):
            raise InvariantViolation(
                "replacement RouteSnapshot must be owner-approved or published"
            )


def utc_now() -> datetime:
    return datetime.now(UTC)


def effect_plan_sha256(effect_kind: str, target: str, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        {"effect_kind": effect_kind, "target": target, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
