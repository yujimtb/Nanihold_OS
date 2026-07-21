from vsm.notifications import (
    AgentNotification,
    AgentNotificationDelivery,
    NotificationPlatform,
    NotificationPromotionRequest,
    NotificationResolutionKind,
    NotificationSourceKind,
    notification_id_for,
)
from vsm.kernel.models import WorkItem, WorkState

from conftest import INTERFACE_NODE_ID, OWNER_ID, SPACE_ID


def addressed_notification(*, requires_work_item: bool) -> AgentNotification:
    notification_id = notification_id_for(
        data_space_id=SPACE_ID,
        source_instance_id="device:intercom",
        source_platform=NotificationPlatform.SLACK,
        source_channel="C123",
        source_message_id="message:one",
        recipient_agent_name="Toki",
        body="@Toki review this",
    )
    return AgentNotification(
        notification_id=notification_id,
        data_space_id=SPACE_ID,
        source_kind=NotificationSourceKind.CHANNEL_INBOUND,
        source_platform=NotificationPlatform.SLACK,
        source_instance_id="device:intercom",
        source_channel="C123",
        source_message_id="message:one",
        source_observation_subject="message:slack:C123-message:one",
        sender_actor_id="U123",
        recipient_agent_name="Toki",
        body="@Toki review this",
        resolution_kind=NotificationResolutionKind.EXPLICIT_MENTION,
        requires_work_item=requires_work_item,
    )


def test_addressed_notification_is_one_shared_ledger_event(system) -> None:
    kernel, ledger, _, _ = system
    notification = addressed_notification(requires_work_item=False)

    receipt = AgentNotificationDelivery(kernel).deliver(
        notification,
        actor_id="system:intercom",
        idempotency_key="notification:one",
    )

    assert receipt.notification_id == notification.notification_id
    event = ledger.page(0, 100)[-1].event
    assert event.event_type == "agent_notification_delivered"
    assert event.payload["notification"]["recipient_agent_name"] == "Toki"
    assert kernel.agent_notifications[notification.notification_id] == notification


def test_promotion_requires_explicit_condition_and_creates_work_item(system) -> None:
    kernel, ledger, _, _ = system
    notification = addressed_notification(requires_work_item=True)
    AgentNotificationDelivery(kernel).deliver(
        notification,
        actor_id="system:intercom",
        idempotency_key="notification:promotion",
    )
    work_item = WorkItem(
        work_item_id="work:notification-promotion",
        data_space_id=SPACE_ID,
        title="Reply to Toki notification",
        description="Draft the requested response.",
        owner_node_id=INTERFACE_NODE_ID,
        delegated_to_node_id=INTERFACE_NODE_ID,
        integration_owner_node_id=INTERFACE_NODE_ID,
        parent_work_item_id=None,
        acceptance_criteria=("The addressed notification is handled.",),
        route_key="route:notification",
        state=WorkState.READY,
        blocking_s3_star_finding_ids=(),
        completion_evidence=None,
    )

    receipt = AgentNotificationDelivery(kernel).promote(
        notification.notification_id,
        NotificationPromotionRequest(work_item=work_item),
        actor_id=OWNER_ID,
        idempotency_key="notification:promotion:command",
    )

    assert receipt.work_item_id == work_item.work_item_id
    assert kernel.work_items[work_item.work_item_id] == work_item
    assert (
        kernel.agent_notifications[notification.notification_id].promoted_work_item_id
        == work_item.work_item_id
    )
    assert [
        stored.event.event_type for stored in ledger.page(0, 100)
    ][-2:] == ["work_item_created", "agent_notification_promoted"]


def test_agent_to_agent_message_uses_the_same_delivery_event(system) -> None:
    kernel, ledger, _, _ = system

    receipt = AgentNotificationDelivery(kernel).send_agent_message(
        data_space_id=SPACE_ID,
        source_instance_id="nanihold:primary",
        sender_actor_id="system:agent-kaba",
        sender_agent_name="Kaba",
        recipient_agent_name="Toki",
        source_message_id="message:agent-one",
        body="Please coordinate.",
        idempotency_key="agent-message:one",
    )

    event = ledger.page(0, 100)[-1].event
    assert receipt.event_id == event.event_id
    assert event.event_type == "agent_notification_delivered"
    assert event.payload["notification"]["source_kind"] == "agent_to_agent"
    assert event.payload["notification"]["sender_agent_name"] == "Kaba"
