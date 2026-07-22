"""Shared agent notification delivery and promotion contracts.

Channel inbound delivery and agent-to-agent messages intentionally use the
same Operational Ledger event shape.  A notification is an auditable event
first; creating a WorkItem is an explicit second command.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from vsm.errors import InvariantViolation
from vsm.ids import validate_id
from vsm.kernel.models import (
    Identifier,
    NonBlank,
    StrictModel,
    WorkItem,
)

if TYPE_CHECKING:
    from vsm.kernel.service import Kernel


class NotificationSourceKind(StrEnum):
    CHANNEL_INBOUND = "channel_inbound"
    AGENT_TO_AGENT = "agent_to_agent"


class NotificationPlatform(StrEnum):
    DISCORD = "discord"
    SLACK = "slack"
    INTERNAL = "internal"


class NotificationResolutionKind(StrEnum):
    EXPLICIT_MENTION = "explicit_mention"
    BOT_REPLY = "bot_reply"
    THREAD_INHERITANCE = "thread_inheritance"
    AGENT_ADDRESS = "agent_address"


class AgentNotification(StrictModel):
    """The durable payload shared by Intercom and the Nanihold kernel."""

    notification_id: Identifier
    data_space_id: Identifier
    source_kind: NotificationSourceKind
    source_platform: NotificationPlatform
    source_instance_id: NonBlank
    source_channel: NonBlank
    source_message_id: NonBlank
    source_observation_subject: NonBlank | None = None
    sender_actor_id: NonBlank
    sender_agent_name: NonBlank | None = None
    recipient_agent_name: NonBlank
    body: str = Field(min_length=1, max_length=262144)
    resolution_kind: NotificationResolutionKind
    requires_work_item: bool
    related_work_item_id: Identifier | None = None
    related_execution_id: Identifier | None = None
    owner_visible: Literal[True] = True
    promoted_work_item_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_source_contract(self) -> "AgentNotification":
        if self.source_kind is NotificationSourceKind.CHANNEL_INBOUND:
            if self.source_platform not in (
                NotificationPlatform.DISCORD,
                NotificationPlatform.SLACK,
            ):
                raise ValueError("channel notifications require Discord or Slack")
            if self.source_observation_subject is None:
                raise ValueError(
                    "channel notifications require the source observation subject"
                )
        elif self.source_platform is not NotificationPlatform.INTERNAL:
            raise ValueError("agent-to-agent notifications require the internal platform")
        if self.source_kind is NotificationSourceKind.AGENT_TO_AGENT:
            if self.sender_agent_name is None:
                raise ValueError("agent-to-agent notifications require a sender name")
            if self.resolution_kind is not NotificationResolutionKind.AGENT_ADDRESS:
                raise ValueError(
                    "agent-to-agent notifications require agent-address resolution"
                )
            if (
                self.related_work_item_id is None
                or self.related_execution_id is None
            ):
                raise ValueError(
                    "agent-to-agent notifications require WorkItem and Execution references"
                )
        if self.promoted_work_item_id is not None and not self.requires_work_item:
            raise ValueError(
                "a notification cannot have a promoted WorkItem without promotion"
            )
        return self


class NotificationPromotionRequest(StrictModel):
    """Explicit WorkItem details required to promote a notification."""

    work_item: WorkItem


class AgentNotificationReceipt(StrictModel):
    notification_id: Identifier
    event_id: Identifier
    work_item_id: Identifier | None = None


def notification_id_for(
    *,
    data_space_id: str,
    source_instance_id: str,
    source_platform: NotificationPlatform,
    source_channel: str,
    source_message_id: str,
    recipient_agent_name: str,
    body: str,
    sender_agent_name: str | None = None,
    related_work_item_id: str | None = None,
    related_execution_id: str | None = None,
) -> str:
    """Return a stable identifier for one addressed message."""

    canonical = json.dumps(
        {
            "data_space_id": data_space_id,
            "source_instance_id": source_instance_id,
            "source_platform": source_platform.value,
            "source_channel": source_channel,
            "source_message_id": source_message_id,
            "recipient_agent_name": recipient_agent_name,
            "body": body,
            "sender_agent_name": sender_agent_name,
            "related_work_item_id": related_work_item_id,
            "related_execution_id": related_execution_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    value = f"notification:{hashlib.sha256(canonical).hexdigest()}"
    return validate_id(value)


class AgentNotificationDelivery:
    """One delivery base for channel and agent-to-agent notifications."""

    def __init__(self, kernel: "Kernel") -> None:
        self._kernel = kernel

    def deliver(
        self,
        notification: AgentNotification,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> AgentNotificationReceipt:
        event = self._kernel.record_agent_notification(
            notification,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        return AgentNotificationReceipt(
            notification_id=notification.notification_id,
            event_id=event.event_id,
        )

    def promote(
        self,
        notification_id: str,
        request: NotificationPromotionRequest,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> AgentNotificationReceipt:
        event = self._kernel.promote_agent_notification(
            notification_id,
            request.work_item,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )
        notification = self._kernel.agent_notifications[notification_id]
        return AgentNotificationReceipt(
            notification_id=notification_id,
            event_id=event.event_id,
            work_item_id=notification.promoted_work_item_id,
        )

    def receive_agent_messages(
        self, recipient_agent_name: str
    ) -> tuple[AgentNotification, ...]:
        """Read the shared Ledger projection for one registry-issued recipient."""

        if not self._kernel.agent_name_is_registered(recipient_agent_name):
            raise InvariantViolation(
                "agent-to-agent recipient must be issued by the agent-name registry"
            )
        return tuple(
            notification
            for notification in self._kernel.agent_notifications.values()
            if notification.source_kind is NotificationSourceKind.AGENT_TO_AGENT
            and notification.recipient_agent_name == recipient_agent_name
        )

    def send_agent_message(
        self,
        *,
        data_space_id: str,
        source_instance_id: str,
        sender_actor_id: str,
        sender_agent_name: str,
        recipient_agent_name: str,
        source_message_id: str,
        body: str,
        related_work_item_id: str,
        related_execution_id: str,
        requires_work_item: bool = False,
        idempotency_key: str,
    ) -> AgentNotificationReceipt:
        notification_id = notification_id_for(
            data_space_id=data_space_id,
            source_instance_id=source_instance_id,
            source_platform=NotificationPlatform.INTERNAL,
            source_channel="operational",
            source_message_id=source_message_id,
            recipient_agent_name=recipient_agent_name,
            body=body,
            sender_agent_name=sender_agent_name,
            related_work_item_id=related_work_item_id,
            related_execution_id=related_execution_id,
        )
        notification = AgentNotification(
            notification_id=notification_id,
            data_space_id=data_space_id,
            source_kind=NotificationSourceKind.AGENT_TO_AGENT,
            source_platform=NotificationPlatform.INTERNAL,
            source_instance_id=source_instance_id,
            source_channel="operational",
            source_message_id=source_message_id,
            sender_actor_id=sender_actor_id,
            sender_agent_name=sender_agent_name,
            recipient_agent_name=recipient_agent_name,
            body=body,
            resolution_kind=NotificationResolutionKind.AGENT_ADDRESS,
            requires_work_item=requires_work_item,
            related_work_item_id=related_work_item_id,
            related_execution_id=related_execution_id,
        )
        return self.deliver(
            notification,
            actor_id=sender_actor_id,
            idempotency_key=idempotency_key,
        )


__all__ = [
    "AgentNotification",
    "AgentNotificationDelivery",
    "AgentNotificationReceipt",
    "NotificationPlatform",
    "NotificationPromotionRequest",
    "NotificationResolutionKind",
    "NotificationSourceKind",
    "notification_id_for",
]
