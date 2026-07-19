from __future__ import annotations

import json
from pathlib import Path

from conftest import INTERFACE_NODE_ID, OWNER_ID, SPACE_ID
from vsm.interface.models import Conversation
from vsm.interface.service import InterfaceService
from vsm.kernel.service import Kernel
from vsm.projection import OperationalProjection


GOLDEN = Path(__file__).parent / "fixtures" / "fable-owner-ux-golden-119.json"


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
            provider_session_id=None,
            last_event_cursor=0,
            status="active",
        ),
        idempotency_key="golden:conversation",
    )

    passed = 0
    for case in manifest["cases"]:
        before = pilot.calls
        response = interface.turn(
            conversation_id="conversation:golden",
            owner_text=f"<owner-message sha256={case['text_sha256']}>",
            idempotency_key=case["case_id"],
            force_new_pilot=False,
        )
        context = pilot.contexts[-1]
        if all(
            (
                pilot.calls == before + 1,
                response.provider_session_id == "provider-session",
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
    rebuilt_interface = InterfaceService(
        kernel=rebuilt_kernel,
        ledger=ledger,
        pilot=pilot,
        clock=kernel.clock,
    )
    projection = OperationalProjection(
        kernel=rebuilt_kernel, interface=rebuilt_interface
    )
    projection.rebuild(page_size=17)
    assert len(rebuilt_interface.messages["conversation:golden"]) == 238
    assert (
        rebuilt_interface.conversations["conversation:golden"].provider_session_id
        == "provider-session"
    )
