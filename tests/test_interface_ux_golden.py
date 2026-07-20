from __future__ import annotations

import json
from pathlib import Path

from conftest import INTERFACE_NODE_ID, OWNER_ID, SPACE_ID
from vsm.interface.models import (
    Conversation,
    MessageSource,
    OwnerMessageAction,
    SurfaceBinding,
)
from vsm.interface.service import InterfaceService
from vsm.kernel.service import Kernel
from vsm.projection import OperationalProjection
from vsm.token_lab.lab import TokenEfficiencyLab, TokenLabEventService


GOLDEN = Path(__file__).parent / "fixtures" / "interface-owner-ux-golden-119.json"


def test_all_119_historical_owner_inputs_keep_the_continuous_interface_contract(system):
    kernel, ledger, interface, pilot = system
    manifest = json.loads(GOLDEN.read_text("utf-8"))
    assert manifest["case_count"] == 119
    assert manifest["short_le_40"] == 53
    assert manifest["context_dependent"] == 67
    interface.create_conversation(
        Conversation(
            conversation_id="conversation:golden",
            data_space_id=SPACE_ID,
            interface_node_id=INTERFACE_NODE_ID,
            owner_id=OWNER_ID,
            title="Golden",
        ),
        SurfaceBinding(
            binding_id="binding:golden",
            conversation_id="conversation:golden",
            surface="web",
            source_session_id="golden",
            channel_id="owner",
            device_id="device:test",
        ),
        idempotency_key="golden:conversation",
    )

    passed = 0
    for case in manifest["cases"]:
        before = pilot.calls
        receipt = interface.perform_owner_action(
            conversation_id="conversation:golden",
            action=OwnerMessageAction(
                action_id=f"action:{case['case_id'].split(':', 1)[-1]}",
                idempotency_key=case["case_id"],
                kind="owner_message",
                text=f"<owner-message sha256={case['text_sha256']}>",
                source=MessageSource(
                    surface="web",
                    source_session_id="golden",
                    source_message_id=case["case_id"],
                    author_id=OWNER_ID,
                    channel_id="owner",
                    occurred_at=kernel.clock(),
                ),
            ),
            device_id="device:test",
        )
        context = pilot.contexts[-1]
        if all(
            (
                pilot.calls == before + 1,
                receipt.status == "completed",
                not hasattr(context, "full_history"),
                context.owner_message_blob_ref.startswith("blob:sha256:"),
                context.event_delta.event_count >= 1,
                (
                    context.resume_pack is not None
                    if before == 0
                    else context.resume_pack is None
                ),
                context.provider_session_id
                == (None if before == 0 else "provider-session"),
            )
        ):
            passed += 1

    assert passed == 119
    assert pilot.calls == 119
    assert len(interface.messages["conversation:golden"]) == 238
    before_status = pilot.calls
    status = interface.status("conversation:golden")
    assert status.model_calls == 0
    assert pilot.calls == before_status

    rebuilt_kernel = Kernel(
        data_space=kernel.data_space,
        ledger=ledger,
        audit_policy=kernel.audit_policy,
        control_policy=kernel.control_policy,
        clock=kernel.clock,
    )
    rebuilt_lab_events = TokenLabEventService(
        lab=TokenEfficiencyLab(),
        ledger=ledger,
        data_space_id=SPACE_ID,
        clock=kernel.clock,
    )
    rebuilt_interface = InterfaceService(
        kernel=rebuilt_kernel,
        ledger=ledger,
        pilot=pilot,
        token_lab_events=rebuilt_lab_events,
        clock=kernel.clock,
    )
    projection = OperationalProjection(
        kernel=rebuilt_kernel,
        interface=rebuilt_interface,
        token_lab_events=rebuilt_lab_events,
    )
    projection.rebuild(page_size=17)
    assert len(rebuilt_interface.messages["conversation:golden"]) == 238
    assert next(iter(rebuilt_interface.pilot_sessions.values())).provider_session_id == (
        "provider-session"
    )
