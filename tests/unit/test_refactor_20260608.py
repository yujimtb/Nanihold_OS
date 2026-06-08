from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from vsm.architecture.events import EventEnvelope
from vsm.architecture.projections import ProjectionCheckpoint
from vsm.authority import ParentAuthority
from vsm.clock import SystemClock
from vsm.eventlog.schema import EVENT_TYPES, EVENT_TYPES_V1, Event, validate_event_payload
from vsm.eventlog.writer import EventLogWriter
from vsm.nodes import DifferentiationLevel, NodeStatus, assert_transition_allowed
from vsm.roles import RoleSpec, SystemRole
from vsm.runtime.topology import LiveTopology, StaticTopologyEntry
from vsm.tools import ToolEffect, ToolInvocation
from vsm.tools.coordination import CoordinationFacade, CoordinationRequest


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


def test_node_lifecycle_and_authority_limits() -> None:
    assert_transition_allowed(NodeStatus.CREATED, NodeStatus.RUNNING)
    with pytest.raises(ValueError):
        assert_transition_allowed(NodeStatus.COMPLETED, NodeStatus.RUNNING)

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


def test_role_spec_and_live_topology() -> None:
    role = RoleSpec(
        id="S5_POLICY",
        vsm_position=SystemRole.S5_POLICY,
        responsibility="policy",
        allowed_tools=("request_escalation",),
    )
    assert role.allowed_tools == ("request_escalation",)

    topology = LiveTopology.from_static(
        [
            StaticTopologyEntry(id="root_s5", role=SystemRole.S5_POLICY),
            StaticTopologyEntry(id="s3_main", role=SystemRole.S3_ALLOCATOR, parent="root_s5"),
        ]
    )
    assert topology.nodes["s3_main"].parent_id == "root_s5"
    topology.apply_event(
        {
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
