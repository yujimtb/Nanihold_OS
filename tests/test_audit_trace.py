from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vsm.agent_naming import AgentNameRegistry, AgentNameRow
from vsm.audit_trace import trace_execution_attribution, trace_notification_delivery, trace_reply_chain
from vsm.errors import InvariantViolation
from vsm.kernel.models import Execution, ExecutionState, NodeKind, WorkItem, WorkState
from vsm.kernel.models import RouteSnapshot, RouteSnapshotState
from vsm.pilot.models import ModelCandidate
from vsm.reply_authoring import ReplyDraftSubmission

from conftest import INTERFACE_NODE_ID, NOW, OWNER_ID, SPACE_ID, make_node


def _notification(*, requires_work_item: bool):
    from vsm.notifications import (
        AgentNotification,
        NotificationPlatform,
        NotificationResolutionKind,
        NotificationSourceKind,
        notification_id_for,
    )

    notification_id = notification_id_for(
        data_space_id=SPACE_ID,
        source_instance_id="device:intercom",
        source_platform=NotificationPlatform.SLACK,
        source_channel="C123",
        source_message_id="message:audit",
        recipient_agent_name="Toki",
        body="@Toki audit this",
    )
    return AgentNotification(
        notification_id=notification_id,
        data_space_id=SPACE_ID,
        source_kind=NotificationSourceKind.CHANNEL_INBOUND,
        source_platform=NotificationPlatform.SLACK,
        source_instance_id="device:intercom",
        source_channel="C123",
        source_message_id="message:audit",
        source_observation_subject="message:slack:C123-message:audit",
        sender_actor_id="U123",
        recipient_agent_name="Toki",
        body="@Toki audit this",
        resolution_kind=NotificationResolutionKind.EXPLICIT_MENTION,
        requires_work_item=requires_work_item,
    )


def test_notification_trace_reconstructs_incoming_to_delivery_and_promotion(system):
    kernel, _, _, _ = system
    notification = _notification(requires_work_item=True)
    delivery = kernel.record_agent_notification(
        notification,
        actor_id="system:intercom",
        idempotency_key="notification:audit",
    )
    work_item = WorkItem(
        work_item_id="work:notification-audit",
        data_space_id=SPACE_ID,
        title="Audit notification",
        description="Keep the notification delivery trace complete.",
        owner_node_id=INTERFACE_NODE_ID,
        delegated_to_node_id=INTERFACE_NODE_ID,
        integration_owner_node_id=INTERFACE_NODE_ID,
        parent_work_item_id=None,
        acceptance_criteria=("The notification delivery is traceable.",),
        route_key="route:notification-audit",
        state=WorkState.READY,
        blocking_s3_star_finding_ids=(),
        completion_evidence=None,
    )
    kernel.promote_agent_notification(
        notification.notification_id,
        work_item,
        actor_id=OWNER_ID,
        idempotency_key="notification:audit:promotion",
    )

    trace = trace_notification_delivery(kernel, notification.notification_id)

    assert trace["verified"] is True
    assert trace["incoming"]["source_message_id"] == "message:audit"
    assert trace["recipient_agent_name"] == "Toki"
    assert trace["delivery"]["kind"] == "ledger_event+work_item"
    assert trace["delivery"]["ledger_event_id"] == delivery.event_id
    assert trace["delivery"]["work_item_id"] == work_item.work_item_id


def test_notification_trace_reports_ledger_only_delivery(system):
    kernel, _, _, _ = system
    notification = _notification(requires_work_item=False)
    kernel.record_agent_notification(
        notification,
        actor_id="system:intercom",
        idempotency_key="notification:audit:ledger-only",
    )

    trace = trace_notification_delivery(kernel, notification.notification_id)

    assert trace["verified"] is True
    assert trace["delivery"]["kind"] == "ledger_event"
    assert trace["delivery"]["work_item_id"] is None


def test_execution_trace_validates_agent_name_work_item_and_receipt(system):
    kernel, _, _, _ = system
    worker = make_node("node:audit-worker", name="Audit worker", kind=NodeKind.UNIT)
    kernel.register_node(worker, actor_id=OWNER_ID, idempotency_key="node:audit-worker")
    work_item = WorkItem(
        work_item_id="work:execution-audit",
        data_space_id=SPACE_ID,
        title="Audit execution",
        description="Keep assignment and receipt linked.",
        owner_node_id=INTERFACE_NODE_ID,
        delegated_to_node_id=worker.node_id,
        integration_owner_node_id=INTERFACE_NODE_ID,
        parent_work_item_id=None,
        acceptance_criteria=("The assignment is traceable.",),
        route_key="route:execution-audit",
        state=WorkState.READY,
        blocking_s3_star_finding_ids=(),
        completion_evidence=None,
    )
    kernel.create_work_item(work_item, actor_id=OWNER_ID, idempotency_key="work:execution-audit")
    candidate = ModelCandidate(
        adapter="test",
        adapter_version="1",
        provider="anthropic",
        selection="exact",
        model_snapshot="claude-opus-4-1",
        effort="high",
        toolset=(),
        sandbox_fingerprint="sandbox:test",
        environment_fingerprint="environment:test",
    )
    execution = Execution(
        execution_id="execution:execution-audit",
        data_space_id=SPACE_ID,
        node_id=worker.node_id,
        work_item_id=work_item.work_item_id,
        pilot_id="pilot:audit",
        model_candidate_key=candidate.key,
        state=ExecutionState.REQUESTED,
        provider_session_id=None,
        pilot_host_id="pilot-host:audit",
        pause_reason=None,
    )
    kernel.create_execution(execution, actor_id=OWNER_ID, idempotency_key="execution:execution-audit")
    names = AgentNameRegistry(
        [
            AgentNameRow(
                category="居",
                scale=2,
                semantic_coordinate="甲",
                japanese_name="Toki",
                english_name="Toki",
                latin_name="Toki",
                likes="1",
            )
        ]
    )
    assignment = names.allocate(
        assignment_id="assignment:execution-audit",
        data_space_id=SPACE_ID,
        work_item_id=work_item.work_item_id,
        execution_id=execution.execution_id,
        node_id=worker.node_id,
        pilot_id=execution.pilot_id,
        candidate=candidate,
    )
    kernel.record_agent_name_assignment(
        assignment,
        naming_registry=names,
        actor_id="system:dispatcher",
        idempotency_key="assignment:execution-audit",
    )
    kernel.record_pilot_execution_receipt(
        execution.execution_id,
        receipt_id="receipt:execution-audit",
        receipt_status="succeeded",
        requested_model=candidate.model_snapshot,
        actual_model=candidate.model_snapshot,
        provider_session_id="provider-session:audit",
        usage={"input_tokens": 1},
        result={"summary": "audited"},
        error=None,
        actor_id=execution.pilot_id,
        idempotency_key="receipt:execution-audit",
    )

    trace = trace_execution_attribution(kernel, execution.execution_id)

    assert trace["verified"] is True
    assert trace["agent_name"] == "Toki"
    assert trace["assignment"]["work_item_id"] == work_item.work_item_id
    assert trace["receipt"]["receipt_id"] == "receipt:execution-audit"
    assert trace["receipt"]["agent_name"] == "Toki"


def test_reply_trace_validates_draft_approval_and_send_record():
    draft = ReplyDraftSubmission.new(
        incoming_observation_id="obs:reply-audit",
        channel="slack",
        recipient="C123",
        body="明示的な返信",
        drafted_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        agent_name="Toki",
        work_item_id="work:reply-audit",
        execution_id="execution:reply-audit",
    )
    draft_record = draft.supplemental()
    approval = {
        "id": "sup:reply-approval-audit",
        "kind": "reply-approval@1",
        "derived_from": {
            "observations": [],
            "blobs": [],
            "supplementals": [draft.draft_id],
        },
        "payload": {"state": "approved"},
    }
    send_record = {
        "id": "sup:send-record-audit",
        "kind": "send-record@1",
        "derived_from": {
            "observations": [],
            "blobs": [],
            "supplementals": [draft.draft_id],
        },
        "payload": {
            "channel": "slack",
            "mode": "approved",
            "approval_id": approval["id"],
            "message_ref": "1710000001.000200",
        },
    }

    trace = trace_reply_chain([draft_record, approval, send_record], draft.draft_id)

    assert trace["verified"] is True
    assert trace["draft"]["agent_name"] == "Toki"
    assert trace["draft"]["incoming_observation_id"] == "obs:reply-audit"
    assert trace["approval"]["id"] == approval["id"]
    assert trace["delivery"]["id"] == send_record["id"]


def test_reply_trace_rejects_unanchored_send_record():
    draft = ReplyDraftSubmission.new(
        incoming_observation_id="obs:reply-audit",
        channel="slack",
        recipient="C123",
        body="明示的な返信",
        drafted_at=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        agent_name="Toki",
        work_item_id="work:reply-audit",
        execution_id="execution:reply-audit",
    )
    approval = {
        "id": "sup:reply-approval-audit",
        "kind": "reply-approval@1",
        "derived_from": {"observations": [], "blobs": [], "supplementals": [draft.draft_id]},
    }
    send_record = {
        "id": "sup:send-record-audit",
        "kind": "send-record@1",
        "derived_from": {"observations": [], "blobs": [], "supplementals": []},
        "payload": {
            "channel": "slack",
            "mode": "approved",
            "approval_id": approval["id"],
            "message_ref": "1710000001.000200",
        },
    }

    with pytest.raises(InvariantViolation, match="send record"):
        trace_reply_chain([draft.supplemental(), approval, send_record], draft.draft_id)
