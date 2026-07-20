from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import INTERFACE_NODE_ID, NOW, OWNER_ID, SPACE_ID, make_node
from vsm.errors import InvariantViolation, ReconciliationRequired
from vsm.kernel.models import (
    CompletionEvidence,
    EffectLease,
    EffectLeaseState,
    Execution,
    ExecutionState,
    NodeKind,
    RouteSnapshot,
    RouteSnapshotState,
    S3StarFinding,
    S3StarSeverity,
    WorkItem,
    WorkState,
    EventEnvelope,
)
from vsm.kernel.service import effect_plan_sha256
from vsm.projection import OperationalProjection


def test_projection_skips_only_lethe_owned_history_events(system):
    kernel, _, interface, _ = system
    projection = OperationalProjection(kernel=kernel, interface=interface)
    history_message = EventEnvelope(
        event_id="event:history-imported",
        data_space_id=SPACE_ID,
        stream_id=f"history-message:{'a' * 64}",
        stream_version=99,
        event_type="history.message_imported",
        occurred_at=NOW,
        actor_type="system",
        actor_id=None,
        correlation_id=None,
        causation_id=None,
        idempotency_key="history:imported",
        payload={},
    )
    projection.apply(history_message)
    projection.apply(
        history_message.model_copy(
            update={
                "event_id": "event:history-import-completed",
                "stream_id": f"history-import:{'b' * 64}",
                "event_type": "history.import_completed",
            }
        )
    )

    wrong_stream = history_message.model_copy(
        update={"event_id": "event:wrong-history-stream", "stream_id": "history:wrong"}
    )
    with pytest.raises(InvariantViolation, match="reserved history stream"):
        projection.apply(wrong_stream)

    unknown = history_message.model_copy(
        update={
            "event_id": "event:unknown",
            "stream_id": f"history-message:{'c' * 64}",
            "stream_version": 1,
            "event_type": "history.unknown",
        }
    )
    with pytest.raises(InvariantViolation, match="no handler"):
        projection.apply(unknown)


def work(work_id: str, node_id: str, parent: str | None = None) -> WorkItem:
    return WorkItem(
        work_item_id=work_id,
        data_space_id=SPACE_ID,
        title=f"Work {work_id}",
        description="Execute and integrate this obligation.",
        owner_node_id=INTERFACE_NODE_ID,
        delegated_to_node_id=node_id,
        integration_owner_node_id=INTERFACE_NODE_ID,
        parent_work_item_id=parent,
        acceptance_criteria=("verified",),
        route_key="coding_s1",
        state=WorkState.READY,
        blocking_s3_star_finding_ids=(),
        completion_evidence=None,
    )


def execution(execution_id: str, work_id: str, node_id: str, host: str) -> Execution:
    return Execution(
        execution_id=execution_id,
        data_space_id=SPACE_ID,
        node_id=node_id,
        work_item_id=work_id,
        pilot_id=f"pilot:{execution_id.split(':')[1]}",
        model_candidate_key="candidate:test",
        state=ExecutionState.ACTIVE,
        provider_session_id=None,
        pilot_host_id=host,
        pause_reason=None,
    )


def test_recursive_uvsm_multiple_executions_effects_and_targeted_intervention(system):
    kernel, _, _, _ = system
    root = make_node("node:company", name="Company", kind=NodeKind.ORGANIZATION)
    child = make_node(
        "node:unit",
        name="Product S1",
        kind=NodeKind.UNIT,
        parent_node_id=root.node_id,
    )
    kernel.register_node(root, actor_id=OWNER_ID, idempotency_key="node:company")
    kernel.register_node(child, actor_id=OWNER_ID, idempotency_key="node:unit")
    parent = work("work:parent", child.node_id)
    delegated = work("work:child", child.node_id, parent.work_item_id)
    other = work("work:other", child.node_id)
    for item in (parent, delegated, other):
        kernel.create_work_item(
            item, actor_id=OWNER_ID, idempotency_key=f"create:{item.work_item_id}"
        )
    kernel.add_dependency(
        work_item_id=parent.work_item_id,
        depends_on_id=delegated.work_item_id,
        actor_id=OWNER_ID,
        idempotency_key="dependency:parent-child",
    )
    with pytest.raises(InvariantViolation):
        kernel.add_dependency(
            work_item_id=delegated.work_item_id,
            depends_on_id=parent.work_item_id,
            actor_id=OWNER_ID,
            idempotency_key="dependency:cycle",
        )
    first = execution("execution:first", delegated.work_item_id, child.node_id, "host:one")
    second = execution("execution:second", delegated.work_item_id, child.node_id, "host:one")
    third = execution("execution:third", other.work_item_id, child.node_id, "host:two")
    for item in (first, second, third):
        kernel.create_execution(
            item, actor_id=OWNER_ID, idempotency_key=f"create:{item.execution_id}"
        )
    lease = EffectLease(
        lease_id="lease:first",
        data_space_id=SPACE_ID,
        work_item_id=delegated.work_item_id,
        execution_id=first.execution_id,
        effect_kind="git_push",
        target="origin/feature",
        idempotency_key="effect:push:one",
        plan_sha256=effect_plan_sha256("git_push", "origin/feature", {"force": False}),
        state=EffectLeaseState.PLANNED,
        expires_at=NOW + timedelta(hours=1),
    )
    kernel.plan_effect(lease, actor_id=first.pilot_id, idempotency_key="plan:lease")
    kernel.approve_effect(
        lease.lease_id, actor_id=OWNER_ID, idempotency_key="approve:lease"
    )
    kernel.activate_effect(
        lease.lease_id, actor_id=first.pilot_id, idempotency_key="activate:lease"
    )
    kernel.mark_effect_unknown(
        lease.lease_id, actor_id=first.pilot_id, idempotency_key="unknown:lease"
    )
    with pytest.raises(ReconciliationRequired):
        kernel.require_effect_reconciliation(lease.lease_id)
    kernel.reconcile_effect(
        lease.lease_id,
        EffectLeaseState.FAILED,
        actor_id=OWNER_ID,
        idempotency_key="reconcile:lease",
    )
    kernel.intervene(
        delegated.work_item_id,
        actor_id=OWNER_ID,
        reason="owner correction",
        idempotency_key="intervene:child",
    )
    assert kernel.executions[first.execution_id].state is ExecutionState.PAUSED
    assert kernel.executions[second.execution_id].state is ExecutionState.PAUSED
    assert kernel.executions[third.execution_id].state is ExecutionState.ACTIVE
    assert kernel.work_items[other.work_item_id].state is WorkState.READY


def test_severe_s3_star_requires_same_level_s5_and_completion_gate(system):
    kernel, _, _, _ = system
    node = make_node("node:worker", name="Worker", kind=NodeKind.UNIT)
    kernel.register_node(node, actor_id=OWNER_ID, idempotency_key="node:worker")
    item = work("work:gate", node.node_id)
    kernel.create_work_item(item, actor_id=OWNER_ID, idempotency_key="work:gate")
    finding = S3StarFinding(
        finding_id="finding:risk",
        data_space_id=SPACE_ID,
        work_item_id=item.work_item_id,
        node_id=node.node_id,
        severity=S3StarSeverity.SEVERE,
        statement="Unverified integration risk.",
        evidence_refs=("artifact:gate",),
        accepted_by_s5=False,
    )
    kernel.record_s3_star_finding(
        finding, actor_id="system:auditor", idempotency_key="finding:risk"
    )
    with pytest.raises(InvariantViolation):
        kernel.accept_s3_star_risk(
            finding.finding_id,
            s5_node_id=INTERFACE_NODE_ID,
            actor_id=OWNER_ID,
            rationale="wrong level",
            idempotency_key="accept:wrong",
        )
    kernel.accept_s3_star_risk(
        finding.finding_id,
        s5_node_id=node.node_id,
        actor_id=OWNER_ID,
        rationale="risk explicitly accepted",
        idempotency_key="accept:right",
    )
    incomplete = CompletionEvidence(
        acceptance_satisfied=True,
        required_tests_passed=True,
        blocking_deviations=(),
        independent_s3_star_gate=True,
        integration_branch_merged=True,
        remote_push_succeeded=False,
    )
    with pytest.raises(InvariantViolation):
        kernel.complete_work_item(
            item.work_item_id,
            incomplete,
            actor_id=OWNER_ID,
            idempotency_key="complete:no-push",
        )
    complete = incomplete.model_copy(update={"remote_push_succeeded": True})
    kernel.complete_work_item(
        item.work_item_id,
        complete,
        actor_id=OWNER_ID,
        idempotency_key="complete:yes",
    )
    assert kernel.work_items[item.work_item_id].state is WorkState.COMPLETED


def test_pilot_disconnect_pauses_only_its_executions(system):
    kernel, _, _, _ = system
    node = make_node("node:pilot", name="Pilot node", kind=NodeKind.UNIT)
    kernel.register_node(node, actor_id=OWNER_ID, idempotency_key="node:pilot")
    for suffix in ("a", "b"):
        item = work(f"work:{suffix}", node.node_id)
        kernel.create_work_item(
            item, actor_id=OWNER_ID, idempotency_key=f"work:{suffix}"
        )
        kernel.create_execution(
            execution(
                f"execution:{suffix}",
                item.work_item_id,
                node.node_id,
                f"host:{suffix}",
            ),
            actor_id=OWNER_ID,
            idempotency_key=f"execution:{suffix}",
        )
    kernel.pilot_host_disconnected("host:a", idempotency_key="disconnect:a")
    assert kernel.executions["execution:a"].state is ExecutionState.PAUSED
    assert kernel.executions["execution:b"].state is ExecutionState.ACTIVE


def test_route_snapshot_approval_order_and_projection_rebuild(system):
    kernel, ledger, interface, pilot = system
    snapshot = RouteSnapshot(
        snapshot_id="route:current",
        data_space_id=SPACE_ID,
        route_key="coding_s1",
        evidence_cursor=1,
        candidate_keys=("candidate:one",),
        production_objective="quality_max",
        state=RouteSnapshotState.DRAFT,
        s3_star_approval_event_id=None,
        owner_approval_event_id=None,
    )
    kernel.register_route_snapshot(
        snapshot, actor_id=OWNER_ID, idempotency_key="route:register"
    )
    with pytest.raises(InvariantViolation):
        kernel.approve_route_snapshot(
            snapshot.snapshot_id,
            approval="owner",
            actor_id=OWNER_ID,
            idempotency_key="route:owner-early",
        )
    kernel.approve_route_snapshot(
        snapshot.snapshot_id,
        approval="s3_star",
        actor_id=OWNER_ID,
        idempotency_key="route:s3",
    )
    kernel.approve_route_snapshot(
        snapshot.snapshot_id,
        approval="owner",
        actor_id=OWNER_ID,
        idempotency_key="route:owner",
    )
    kernel.publish_route_snapshot(
        snapshot.snapshot_id,
        actor_id=OWNER_ID,
        idempotency_key="route:publish",
    )

    from vsm.interface.service import InterfaceService
    from vsm.kernel.service import Kernel

    rebuilt_kernel = Kernel(
        data_space=kernel.data_space,
        ledger=ledger,
        audit_policy=kernel.audit_policy,
        control_policy=kernel.control_policy,
        clock=kernel.clock,
    )
    rebuilt_interface = InterfaceService(
        kernel=rebuilt_kernel,
        ledger=ledger,
        pilot=pilot,
        token_lab_events=interface.token_lab_events,
        clock=kernel.clock,
    )
    projection = OperationalProjection(
        kernel=rebuilt_kernel, interface=rebuilt_interface
    )
    projection.rebuild(page_size=2)
    assert rebuilt_kernel.route_snapshots[snapshot.snapshot_id] == (
        kernel.route_snapshots[snapshot.snapshot_id]
    )
    assert rebuilt_kernel.nodes == kernel.nodes
    assert projection.cursor == len(ledger.page(0, 1000))
