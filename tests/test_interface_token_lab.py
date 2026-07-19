from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import INTERFACE_NODE_ID, NOW, OWNER_ID, SPACE_ID
from vsm.errors import InvariantViolation
from vsm.interface.models import (
    Conversation,
    CreateWorkItemAction,
    MessageSource,
    OwnerMessageAction,
    SurfaceBinding,
)
from vsm.kernel.models import NodeKind, WorkItem, WorkState
from conftest import make_node
from vsm.token_lab.lab import (
    TokenBaseline,
    TokenEfficiencyLab,
    TokenLabEventService,
    TokenIncidentKind,
    TokenObservation,
)


def conversation() -> Conversation:
    return Conversation(
        conversation_id="conversation:main",
        data_space_id=SPACE_ID,
        interface_node_id=INTERFACE_NODE_ID,
        owner_id=OWNER_ID,
        title="Main",
    )


def binding() -> SurfaceBinding:
    return SurfaceBinding(
        binding_id="binding:main",
        conversation_id="conversation:main",
        surface="web",
        source_session_id="web-main",
        channel_id="owner",
        device_id="device:test",
    )


def action(suffix: str, text: str) -> OwnerMessageAction:
    return OwnerMessageAction(
        action_id=f"action:{suffix}",
        idempotency_key=f"turn:{suffix}",
        kind="owner_message",
        text=text,
        source=MessageSource(
            surface="web",
            source_session_id="web-main",
            source_message_id=f"source:{suffix}",
            author_id=OWNER_ID,
            channel_id="owner",
            occurred_at=NOW,
        ),
    )


def test_owner_message_is_persisted_first_one_call_and_status_is_model_free(system):
    _, ledger, interface, pilot = system
    interface.create_conversation(
        conversation(), binding(), idempotency_key="conversation:create"
    )
    first = interface.perform_owner_action(
        conversation_id="conversation:main",
        action=action("one", "続けて"),
        device_id="device:test",
    )
    second = interface.perform_owner_action(
        conversation_id="conversation:main",
        action=action("two", "そこではなくこちらです"),
        device_id="device:test",
    )
    assert first.interface_message.display_text == "accepted:続けて"
    assert second.interface_message.display_text.startswith("accepted:")
    assert pilot.calls == 2
    assert pilot.contexts[0].provider_session_id is None
    assert pilot.contexts[0].resume_pack is not None
    assert pilot.contexts[1].provider_session_id == "provider-session"
    assert pilot.contexts[1].resume_pack is None
    assert pilot.contexts[1].event_delta.event_count == 2
    assert pilot.contexts[1].event_delta.event_type_counts == {
        "owner_message_received": 1,
        "token_observation_recorded": 1,
    }
    assert all(not hasattr(context, "full_history") for context in pilot.contexts)
    status = interface.status("conversation:main")
    assert status.model_calls == 0
    assert pilot.calls == 2
    owner_events = [
        item
        for item in ledger.page(0, 100)
        if item.event.event_type == "owner_message_received"
    ]
    assert len(owner_events) == 2
    assert len(interface.token_lab_events.lab.observations) == 2


def test_lethe_failure_prevents_interface_pilot_call(system):
    _, ledger, interface, pilot = system
    interface.create_conversation(
        conversation(), binding(), idempotency_key="conversation:create"
    )

    def fail(_data):
        raise InvariantViolation("LETHE unavailable")

    ledger.put_blob = fail  # type: ignore[method-assign]
    with pytest.raises(InvariantViolation):
        interface.perform_owner_action(
            conversation_id="conversation:main",
            action=action("failure", "do not lose this"),
            device_id="device:test",
        )
    assert pilot.calls == 0


def test_typed_work_directive_materializes_work_item(system):
    kernel, _, interface, pilot = system
    worker = make_node("node:directive-worker", name="Worker", kind=NodeKind.UNIT)
    kernel.register_node(
        worker, actor_id=OWNER_ID, idempotency_key="node:directive-worker"
    )
    item = WorkItem(
        work_item_id="work:from-interface",
        data_space_id=SPACE_ID,
        title="Materialized",
        description="Created from one Fable response.",
        owner_node_id=INTERFACE_NODE_ID,
        delegated_to_node_id=worker.node_id,
        integration_owner_node_id=INTERFACE_NODE_ID,
        parent_work_item_id=None,
        acceptance_criteria=("exists",),
        route_key="coding_s1",
        state=WorkState.READY,
        blocking_s3_star_finding_ids=(),
        completion_evidence=None,
    )
    pilot.actions = (
        CreateWorkItemAction(
            action_id="interface-action:create-work",
            kind="work_item.create",
            work_item=item,
            depends_on_work_item_ids=(),
        ),
    )
    interface.create_conversation(
        conversation(), binding(), idempotency_key="directive:conversation"
    )
    receipt = interface.perform_owner_action(
        conversation_id="conversation:main",
        action=action("directive", "実装してください"),
        device_id="device:test",
    )
    assert receipt.status == "completed"
    assert kernel.work_items[item.work_item_id] == item


def test_interface_usage_automatically_records_classifier_incident(system):
    _, _, interface, pilot = system
    pilot.classifier_triggered = True
    interface.create_conversation(
        conversation(), binding(), idempotency_key="classifier:conversation"
    )
    interface.perform_owner_action(
        conversation_id="conversation:main",
        action=action("classifier", "確認"),
        device_id="device:test",
    )
    observation = interface.token_lab_events.lab.observations[-1]
    assert TokenIncidentKind.PERMISSION_CLASSIFIER in observation.incident_kinds


def observation(index: int, tokens: int, incidents=()):
    return TokenObservation(
        observation_id=f"observation:{index}",
        work_type="coding",
        occurred_at=NOW + timedelta(minutes=index),
        total_input_tokens=tokens,
        interface_input_tokens=tokens // 2,
        incident_kinds=frozenset(incidents),
        full_history_resent=False,
        expensive_interface_calls=1,
        verified_complete=True,
    )


def test_token_lab_uses_logic_first_and_counts_classifier_misoperation():
    lab = TokenEfficiencyLab()
    lab.approve_baseline(
        TokenBaseline(
            work_type="coding",
            approved_mean_input_tokens=100,
            approved_mean_interface_tokens=50,
            approved_at=NOW,
        )
    )
    direct = lab.observe(
        observation(
            0,
            100,
            (
                TokenIncidentKind.PERMISSION_CLASSIFIER,
                TokenIncidentKind.REEDIT,
            ),
        )
    )
    assert [item.reason for item in direct] == [
        "single_event:permission_classifier"
    ]
    assert direct[0].logic_only
    with pytest.raises(InvariantViolation):
        lab.authorize_model_experiment(
            direct[0], model="claude-fable-5", effort="low"
        )
    with pytest.raises(InvariantViolation):
        lab.authorize_model_experiment(
            direct[0], model="gpt-5.6-luna", effort="high"
        )
    allowed = lab.authorize_model_experiment(
        direct[0], model="gpt-5.6-luna", effort="low"
    )
    assert not allowed.logic_only
    triggers = []
    for index in range(1, 20):
        triggers.extend(lab.observe(observation(index, 120)))
    assert any(
        item.reason == "twenty_item_mean_degraded_by_ten_percent"
        for item in triggers
    )


def test_token_acceptance_requires_every_zero_incident_gate():
    lab = TokenEfficiencyLab()
    report = lab.acceptance_report(
        before_total_input=1000,
        after_total_input=500,
        before_interface_input=1000,
        after_interface_input=300,
        ux_golden_passed=119,
        ux_golden_total=119,
        sandbox_classifier_triggers=0,
    )
    assert report.accepted


def test_token_lab_event_service_persists_before_applying(system):
    kernel, ledger, _, _ = system
    lab = TokenEfficiencyLab()
    service = TokenLabEventService(
        lab=lab,
        ledger=ledger,
        data_space_id=kernel.data_space.data_space_id,
        clock=kernel.clock,
    )
    baseline = TokenBaseline(
        work_type="coding",
        approved_mean_input_tokens=100,
        approved_mean_interface_tokens=50,
        approved_at=NOW,
    )
    service.approve_baseline(
        baseline,
        actor_id="owner:primary",
        idempotency_key="lab:baseline",
    )
    _, triggers = service.observe(
        observation(
            0,
            100,
            (TokenIncidentKind.PERMISSION_CLASSIFIER,),
        ),
        actor_id="system:token-lab",
        idempotency_key="lab:observation",
    )
    assert triggers[0].logic_only
    assert lab.baselines["coding"] == baseline
    assert lab.observations[0].observation_id == "observation:0"
    assert [item.event.event_type for item in ledger.page(1, 10)] == [
        "token_baseline_approved",
        "token_observation_recorded",
    ]
    assert not lab.weekly_due(NOW + timedelta(days=6))
    assert lab.weekly_due(NOW + timedelta(days=7))
    service.record_weekly_review(
        NOW + timedelta(days=7),
        actor_id="system:token-lab",
        idempotency_key="lab:weekly",
    )
    assert not lab.weekly_due(NOW + timedelta(days=13))
    assert lab.weekly_due(NOW + timedelta(days=14))
