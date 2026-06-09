from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from vsm.architecture.events import EventEnvelope
from vsm.architecture.projections import ProjectionCheckpoint
from vsm.authority import Lease, ParentAuthority
from vsm.clock import SystemClock
from vsm.eventlog.schema import EVENT_TYPES, EVENT_TYPES_V1, Event, validate_event_payload
from vsm.eventlog.writer import EventLogWriter
from vsm.memory import ContextView, SearchScope, TaskSummary
from vsm.nodes import DifferentiationLevel, Node, NodeSource, NodeStatus, assert_transition_allowed
from vsm.runtime import Execution, ExecutionStatus
from vsm.runtime.lifecycle import _role_spec_for_system_role
from vsm.roles import RoleSpec, SystemRole
from vsm.runtime.topology import LiveTopology, StaticTopologyEntry
from vsm.tools import (
    DifferentiationFacade,
    DifferentiationRequest,
    EscalationFacade,
    EscalationRequest,
    ToolEffect,
    ToolInvocation,
)
from vsm.tools.coordination import CoordinationFacade, CoordinationRequest
from vsm.agents import AgentSpec, PromptTemplate


def test_event_types_keep_legacy_set_and_add_v1() -> None:
    assert len(EVENT_TYPES) == 26
    assert "node_created" in EVENT_TYPES_V1
    Event(
        ts="2026-01-01T00:00:00.000Z",
        run_id="run-x",
        seq=0,
        event_type="node_created",
        payload={"node_id": "n1"},
    )
    validate_event_payload("tool_invoked", {"tool_name": "llm_call"})


@pytest.mark.asyncio
async def test_writer_emits_event_envelope_v1(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    writer = EventLogWriter(run_id="run-v1", path=path, clock=SystemClock())
    await writer.start()
    try:
        await writer.append(
            "node_created",
            {"node_id": "node-a"},
            node_id="node-a",
            actor_type="system",
            actor_id="root",
        )
        await asyncio.sleep(0.2)
    finally:
        await writer.stop()

    event = json.loads(path.read_text(encoding="utf-8").strip())
    envelope = EventEnvelope.from_event_dict(event)
    assert envelope.event_id
    assert envelope.stream_id == "node-a"
    assert envelope.stream_version == 1
    assert envelope.schema_version == 1
    assert envelope.actor_id == "root"


def test_projection_checkpoint_is_idempotent() -> None:
    event = EventEnvelope(
        event_id="e1",
        seq=1,
        run_id="run-x",
        stream_id="n1",
        stream_version=1,
        event_type="node_created",
        schema_version=1,
        ts="2026-01-01T00:00:00.000Z",
        actor_type="system",
        payload={},
    )
    checkpoint = ProjectionCheckpoint("live_topology", 1)
    assert checkpoint.should_apply(event)
    checkpoint.mark_applied(event)
    assert not checkpoint.should_apply(event)
    replayed_later = event.model_copy(update={"seq": 2})
    assert not checkpoint.should_apply(replayed_later)


def test_node_lifecycle_and_authority_limits() -> None:
    assert_transition_allowed(NodeStatus.CREATED, NodeStatus.RUNNING)
    with pytest.raises(ValueError):
        assert_transition_allowed(NodeStatus.COMPLETED, NodeStatus.RUNNING)

    static_node = Node(
        id="static",
        parent_id=None,
        vsm_position=SystemRole.S5_POLICY,
        terminable=False,
        source=NodeSource.CONFIG,
    )
    assert static_node.is_static
    with pytest.raises(ValueError):
        Node(
            id="invalid-static",
            parent_id=None,
            vsm_position=SystemRole.S5_POLICY,
            terminable=False,
        )

    authority = ParentAuthority(
        authority_id="auth-1",
        issuer_node_id="parent",
        subject_node_id="child",
        issued_at=datetime.now(timezone.utc),
        may_differentiate_to=DifferentiationLevel.PARTIAL,
        allowed_tool_classes=frozenset({ToolEffect.PURE_READ}),
    )
    assert authority.allows_tool_effect(ToolEffect.PURE_READ)
    assert not authority.allows_tool_effect(ToolEffect.CONTROL)
    with pytest.raises(PermissionError):
        authority.assert_can_differentiate_to(DifferentiationLevel.FULL)


def test_tool_effect_idempotency_contract() -> None:
    ToolInvocation(
        invocation_id="i1",
        tool_name="read",
        effect=ToolEffect.PURE_READ,
        requested_by_node_id="n1",
    )
    with pytest.raises(ValueError):
        ToolInvocation(
            invocation_id="i2",
            tool_name="spawn_child",
            effect=ToolEffect.CONTROL,
            requested_by_node_id="n1",
        )


def test_differentiation_facade_enforces_authority() -> None:
    authority = ParentAuthority(
        authority_id="auth-diff",
        issuer_node_id="parent",
        subject_node_id="child",
        issued_at=datetime.now(timezone.utc),
        may_differentiate_to=DifferentiationLevel.PARTIAL,
    )
    facade = DifferentiationFacade()
    request = DifferentiationRequest(
        differentiation_key="diff-1",
        node_id="child",
        requested_by="child",
        target_level=DifferentiationLevel.PARTIAL,
    )

    first = facade.differentiate(request, authority)
    second = facade.differentiate(request, authority)

    assert first.tool_name == "differentiate"
    assert first.idempotency_key == "diff-1"
    assert first.payload["result"] == second.payload["result"]

    with pytest.raises(PermissionError):
        facade.differentiate(
            DifferentiationRequest(
                differentiation_key="diff-2",
                node_id="child",
                requested_by="child",
                target_level=DifferentiationLevel.FULL,
            ),
            authority,
        )


def test_role_spec_and_live_topology() -> None:
    role = RoleSpec(
        id="S5_POLICY",
        vsm_position=SystemRole.S5_POLICY,
        responsibility="policy",
        allowed_tools=("request_escalation",),
    )
    assert role.allowed_tools == ("request_escalation",)
    assert role.spec_id == "S5_POLICY"
    assert role.spec_version == 1

    topology = LiveTopology.from_static(
        [
            StaticTopologyEntry(id="root_s5", role=SystemRole.S5_POLICY),
            StaticTopologyEntry(id="s3_main", role=SystemRole.S3_ALLOCATOR, parent="root_s5"),
        ]
    )
    assert topology.nodes["s3_main"].parent_id == "root_s5"
    topology.apply_event(
        {
            "event_id": "event-node-sales",
            "event_type": "node_created",
            "payload": {
                "node_id": "sales",
                "parent_id": "s3_main",
                "vsm_position": "S1_WORKER",
                "terminable": True,
            },
        }
    )
    assert "sales" in topology.nodes["s3_main"].child_ids
    topology.apply_event(
        {
            "event_id": "event-node-sales",
            "event_type": "node_created",
            "payload": {"node_id": "sales", "parent_id": "s3_main"},
        }
    )
    assert topology.nodes["s3_main"].child_ids.count("sales") == 1
    topology.apply_event(
        {
            "event_id": "event-node-sales-suspended",
            "event_type": "node_suspended",
            "payload": {"node_id": "sales"},
        }
    )
    assert topology.nodes["sales"].status is NodeStatus.SUSPENDED


def test_all_system_roles_attach_codex_run_tool() -> None:
    for role in SystemRole:
        role_spec = _role_spec_for_system_role(role)
        assert "codex_run" in role_spec.allowed_tools


def test_execution_and_spec_versioning_contract() -> None:
    agent_spec = AgentSpec(
        spec_id="agent-s5",
        spec_version=2,
        model_spec="fake/model",
    )
    prompt = PromptTemplate(
        spec_id="prompt-s5",
        spec_version=3,
        body="Decide policy",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    execution = Execution(
        execution_id="exec-1",
        run_id="run-x",
        node_id="s5",
        agent_invocation_id="agent-inv-1",
        status=ExecutionStatus.RUNNING,
    )

    assert agent_spec.spec_version == 2
    assert prompt.spec_id == "prompt-s5"
    assert execution.status is ExecutionStatus.RUNNING
    with pytest.raises(ValueError):
        Execution(execution_id="exec-2", run_id="run-x", node_id="s5")


def test_context_view_uses_refs_and_default_local_search_scope() -> None:
    summary = TaskSummary(goal_achieved=True, approach="reuse child summary")
    view = ContextView(
        node_id="s5",
        run_id="run-x",
        event_refs=("event-1",),
        summary_refs=("summary-1",),
        artifact_refs=("artifact-1",),
        decision_refs=("decision-1",),
    )

    assert summary.goal_achieved
    assert view.search_scope is SearchScope.DIRECT_CHILD_SUMMARIES
    assert view.event_refs == ("event-1",)


def test_coordination_facade_is_idempotent() -> None:
    facade = CoordinationFacade()
    request = CoordinationRequest(
        coordination_key="coord-1",
        scope="run",
        participants=("s1-a", "s1-b"),
        issue="same work item",
        requested_by="s2",
    )
    first = facade.request_coordination(request)
    second = facade.request_coordination(request)
    assert first.idempotency_key == "coord-1"
    assert second.idempotency_key == "coord-1"
    assert first.payload["result"] == second.payload["result"]


def test_escalation_facade_and_lease_contract() -> None:
    facade = EscalationFacade()
    request = EscalationRequest(
        escalation_key="esc-1",
        reason="budget_exceeded",
        blocking_issue="need more token budget",
        requested_by="worker",
        target_authority="s3",
    )

    first = facade.request_escalation(request)
    second = facade.request_escalation(request)

    assert first.tool_name == "request_escalation"
    assert first.idempotency_key == "esc-1"
    assert first.effect is ToolEffect.CONTROL
    assert first.payload["result"] == second.payload["result"]

    lease = Lease(
        lease_id="lease-1",
        owner_node_id="worker",
        resource_ref="external:api",
        lease_expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert lease.is_expired(datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert not lease.is_expired(datetime(2025, 12, 31, tzinfo=timezone.utc))
