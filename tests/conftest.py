from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vsm.interface.service import InterfaceService
from vsm.kernel.ledger import InMemoryOperationalLedger
from vsm.kernel.models import (
    AuditPolicy,
    ControlPolicy,
    DataSpace,
    DataSpaceKind,
    NodeKind,
    NodeStatus,
    UVSMNode,
    VSMFunction,
)
from vsm.kernel.service import Kernel
from vsm.pilot.models import InterfacePilotUsage, StructuredInterfaceResponse


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
SPACE_ID = "space:personal"
OWNER_ID = "owner:primary"
INTERFACE_NODE_ID = "node:interface"


class FakePilot:
    def __init__(self) -> None:
        self.calls = 0
        self.contexts = []

    def respond(self, *, owner_text, context):
        self.calls += 1
        self.contexts.append(context)
        return StructuredInterfaceResponse(
            display_text=f"accepted:{owner_text}",
            work_directives=(),
            decisions=(),
            commitment_updates=(),
            provider_session_id="provider-session",
            pilot_usage=InterfacePilotUsage(
                candidate_key="fake@1:test",
                actual_provider="test",
                actual_model_snapshot="fake",
                input_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                output_tokens=5,
                cost_usd=0.001,
                duration_ms=50,
            ),
        )


def make_node(
    node_id: str,
    *,
    name: str,
    kind: NodeKind,
    parent_node_id: str | None = None,
) -> UVSMNode:
    return UVSMNode(
        node_id=node_id,
        data_space_id=SPACE_ID,
        owner_id=OWNER_ID,
        name=name,
        kind=kind,
        parent_node_id=parent_node_id,
        resident_functions=frozenset(VSMFunction),
        resident_s3_parent_function=VSMFunction.S5,
        status=NodeStatus.ACTIVE,
        memory_stream_id=f"memory:{node_id.split(':', 1)[1]}",
    )


@pytest.fixture
def system():
    ledger = InMemoryOperationalLedger(SPACE_ID)
    data_space = DataSpace(
        data_space_id=SPACE_ID,
        owner_id=OWNER_ID,
        kind=DataSpaceKind.PERSONAL,
        lethe_location="https://lethe.test/personal",
    )
    kernel = Kernel(
        data_space=data_space,
        ledger=ledger,
        audit_policy=AuditPolicy(
            policy_id="policy:audit",
            data_space_id=SPACE_ID,
            independent_s3_star_required=True,
            raw_drill_down_required=True,
            retention_days=3650,
        ),
        control_policy=ControlPolicy(
            policy_id="policy:control",
            data_space_id=SPACE_ID,
            stop_scope="affected_work_and_effects",
            severe_finding_requires_s5_risk_acceptance=True,
            completion_requires_remote_push=True,
        ),
        clock=lambda: NOW,
    )
    interface_node = make_node(
        INTERFACE_NODE_ID, name="Owner interface", kind=NodeKind.INTERFACE
    )
    kernel.register_node(
        interface_node,
        actor_id=OWNER_ID,
        idempotency_key="fixture:interface-node",
    )
    pilot = FakePilot()
    interface = InterfaceService(
        kernel=kernel,
        ledger=ledger,
        pilot=pilot,
        clock=lambda: NOW,
    )
    return kernel, ledger, interface, pilot
