"""ACR-04 audit traces across notification, reply, and work execution records.

The trace reader is deliberately read-only.  It reconstructs links from the
append-only Operational Ledger and, for replies, from the supplemental
envelopes returned by LETHE.  It never appends an audit event while reading.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from vsm.errors import InvariantViolation
from vsm.ids import validate_id
from vsm.kernel.models import EventEnvelope
from vsm.kernel.service import Kernel
from vsm.notifications import AgentNotification

REPLY_DRAFT_KIND = "reply-draft@1"
REPLY_APPROVAL_KIND = "reply-approval@1"
SEND_RECORD_KIND = "send-record@1"

_LINEAGE = re.compile(
    r"^nanihold/work-item/(?P<work_item_id>[^/]+)/execution/"
    r"(?P<execution_id>[^/]+)/agent/(?P<agent_name>[A-Za-z0-9][A-Za-z0-9_-]*)$"
)


def _required_string(value: object, *, field: str, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvariantViolation(f"{context} requires a non-empty {field}")
    return value


def _record_id(record: Mapping[str, object], *, context: str) -> str:
    value = _required_string(record.get("id"), field="id", context=context)
    try:
        value = validate_id(value)
    except InvariantViolation as exc:
        raise InvariantViolation(
            f"{context} id must be a valid sup: identifier"
        ) from exc
    if not value.startswith("sup:"):
        raise InvariantViolation(f"{context} id must use the sup: namespace")
    return value


def _kind(record: Mapping[str, object], *, context: str) -> str:
    return _required_string(record.get("kind"), field="kind", context=context)


def _derived_supplementals(
    record: Mapping[str, object], *, context: str
) -> list[str]:
    derived_from = record.get("derived_from")
    if not isinstance(derived_from, Mapping):
        raise InvariantViolation(f"{context} derived_from is malformed")
    values = derived_from.get("supplementals")
    if not isinstance(values, list) or any(
        not isinstance(item, str) or not item for item in values
    ):
        raise InvariantViolation(f"{context} derived_from.supplementals is malformed")
    return values


def _event_ref(stored: object) -> dict[str, object]:
    event = stored.event
    return {
        "cursor": stored.cursor,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "stream_id": event.stream_id,
        "stream_version": event.stream_version,
        "occurred_at": event.occurred_at.isoformat(),
    }


class AuditTraceService:
    """Reconstruct and validate ACR-04 links without mutating the Kernel."""

    def __init__(self, kernel: Kernel, *, page_size: int = 1000) -> None:
        if page_size <= 0:
            raise InvariantViolation("audit trace page_size must be positive")
        self.kernel = kernel
        self.page_size = page_size

    def _events(self) -> tuple[object, ...]:
        events: list[object] = []
        cursor = 0
        while True:
            page = self.kernel.ledger.page(cursor, self.page_size)
            if not page:
                return tuple(events)
            for stored in page:
                if stored.cursor != cursor + 1:
                    raise InvariantViolation(
                        "audit trace Event Ledger cursor is not contiguous"
                    )
                events.append(stored)
                cursor = stored.cursor

    @staticmethod
    def _payload_mapping(event: EventEnvelope, *, context: str) -> Mapping[str, object]:
        if not isinstance(event.payload, Mapping):
            raise InvariantViolation(f"{context} payload is malformed")
        return event.payload

    def trace_notification(self, notification_id: str) -> dict[str, object]:
        """Return the inbound observation/name/delivery chain for one notification."""

        notification = self.kernel.agent_notifications.get(notification_id)
        if notification is None:
            raise InvariantViolation(f"notification_id not found: {notification_id}")
        delivered: list[object] = []
        promotions: list[object] = []
        for stored in self._events():
            event = stored.event
            payload = self._payload_mapping(event, context=event.event_type)
            if event.event_type == "agent_notification_delivered":
                raw = payload.get("notification")
                if not isinstance(raw, Mapping):
                    raise InvariantViolation(
                        "agent_notification_delivered notification payload is malformed"
                    )
                if raw.get("notification_id") == notification_id:
                    delivered.append(stored)
            elif event.event_type == "agent_notification_promoted":
                if event.stream_id == notification_id or payload.get(
                    "notification_id"
                ) == notification_id:
                    promotions.append(stored)
        if len(delivered) != 1:
            raise InvariantViolation(
                "notification delivery trace requires exactly one delivery event"
            )
        if len(promotions) > 1:
            raise InvariantViolation(
                "notification delivery trace has multiple promotion events"
            )
        if notification.promoted_work_item_id is not None and not promotions:
            raise InvariantViolation(
                "notification projection has a WorkItem promotion without its Event"
            )

        delivery_event = delivered[0]
        delivery_payload = self._payload_mapping(
            delivery_event.event, context="agent_notification_delivered"
        )
        raw_notification = delivery_payload.get("notification")
        if not isinstance(raw_notification, Mapping):
            raise InvariantViolation("notification delivery payload is malformed")
        projected_notification = AgentNotification.model_validate(raw_notification)
        delivered_projection = notification.model_copy(
            update={"promoted_work_item_id": None}
        )
        if projected_notification != delivered_projection:
            raise InvariantViolation(
                "notification delivery payload does not match its projection"
            )
        if raw_notification.get("recipient_agent_name") != notification.recipient_agent_name:
            raise InvariantViolation(
                "notification delivery recipient does not match its projection"
            )
        if raw_notification.get("source_message_id") != notification.source_message_id:
            raise InvariantViolation(
                "notification delivery source message does not match its projection"
            )

        promotion = promotions[0] if promotions else None
        work_item_id = notification.promoted_work_item_id
        if promotion is not None:
            promotion_payload = self._payload_mapping(
                promotion.event, context="agent_notification_promoted"
            )
            promoted = promotion_payload.get("work_item_id")
            if not isinstance(promoted, str) or not promoted:
                raise InvariantViolation(
                    "notification promotion does not contain a WorkItem ID"
                )
            if work_item_id != promoted:
                raise InvariantViolation(
                    "notification promotion does not match its projection"
                )
            if promoted not in self.kernel.work_items:
                raise InvariantViolation(
                    f"notification promotion WorkItem not found: {promoted}"
                )

        delivery_kind = (
            "ledger_event+work_item" if promotion is not None else "ledger_event"
        )

        return {
            "trace_kind": "notification_delivery",
            "notification_id": notification_id,
            "incoming": {
                "source_observation_subject": notification.source_observation_subject,
                "source_instance_id": notification.source_instance_id,
                "source_platform": notification.source_platform,
                "source_channel": notification.source_channel,
                "source_message_id": notification.source_message_id,
                "sender_actor_id": notification.sender_actor_id,
            },
            "recipient_agent_name": notification.recipient_agent_name,
            "delivery": {
                "kind": delivery_kind,
                "ledger_event_id": delivery_event.event.event_id,
                "ledger_cursor": delivery_event.cursor,
                "work_item_id": work_item_id,
                "receipt": {
                    "notification_id": notification_id,
                    "event_id": delivery_event.event.event_id,
                    "work_item_id": work_item_id,
                },
            },
            "timeline": [
                _event_ref(delivery_event),
                *([] if promotion is None else [_event_ref(promotion)]),
            ],
            "verified": True,
        }

    def trace_execution(self, execution_id: str) -> dict[str, object]:
        """Return and validate the individual-name/WorkItem/receipt chain."""

        execution = self.kernel.executions.get(execution_id)
        if execution is None:
            raise InvariantViolation(f"execution_id not found: {execution_id}")
        work_item = self.kernel.work_items.get(execution.work_item_id)
        if work_item is None:
            raise InvariantViolation(
                f"execution WorkItem not found: {execution.work_item_id}"
            )

        assignment_events: list[object] = []
        receipt_events: list[object] = []
        for stored in self._events():
            event = stored.event
            payload = self._payload_mapping(event, context=event.event_type)
            if event.event_type == "agent_name_assigned":
                raw = payload.get("assignment")
                if not isinstance(raw, Mapping):
                    raise InvariantViolation("agent name assignment payload is malformed")
                if raw.get("execution_id") == execution_id:
                    assignment_events.append(stored)
            elif event.event_type == "pilot_execution_receipt_recorded":
                if event.stream_id == execution_id or payload.get("execution_id") == execution_id:
                    receipt_events.append(stored)

        if execution.agent_name is None:
            raise InvariantViolation(
                f"execution has no assigned individual name: {execution_id}"
            )
        if len(assignment_events) != 1:
            raise InvariantViolation(
                "execution audit trace requires exactly one name assignment event"
            )
        if len(receipt_events) != 1:
            raise InvariantViolation(
                "execution audit trace requires exactly one Pilot receipt event"
            )

        assignment_event = assignment_events[0]
        assignment_payload = self._payload_mapping(
            assignment_event.event, context="agent_name_assigned"
        )
        assignment = assignment_payload.get("assignment")
        if not isinstance(assignment, Mapping):
            raise InvariantViolation("agent name assignment payload is malformed")
        assignment_id = _required_string(
            assignment.get("assignment_id"),
            field="assignment_id",
            context="agent name assignment",
        )
        assignment_name = _required_string(
            assignment.get("agent_name"),
            field="agent_name",
            context="agent name assignment",
        )
        if assignment_name != execution.agent_name:
            raise InvariantViolation(
                "assigned individual name does not match Execution"
            )
        if assignment.get("work_item_id") != execution.work_item_id:
            raise InvariantViolation("assignment WorkItem does not match Execution")
        if assignment.get("execution_id") != execution_id:
            raise InvariantViolation("assignment Execution does not match trace subject")

        receipt_event = receipt_events[0]
        receipt_payload = self._payload_mapping(
            receipt_event.event, context="pilot_execution_receipt_recorded"
        )
        receipt_name = receipt_payload.get("agent_name")
        if receipt_name != assignment_name:
            raise InvariantViolation(
                "Pilot receipt individual name does not match assignment"
            )
        receipt_work_item = receipt_payload.get("work_item_id", execution.work_item_id)
        if receipt_work_item != work_item.work_item_id:
            raise InvariantViolation("Pilot receipt WorkItem does not match assignment")
        receipt_execution = receipt_payload.get("execution_id", execution_id)
        if receipt_execution != execution_id:
            raise InvariantViolation("Pilot receipt Execution does not match assignment")
        receipt_id = _required_string(
            receipt_payload.get("receipt_id"),
            field="receipt_id",
            context="Pilot receipt",
        )

        return {
            "trace_kind": "execution_attribution",
            "execution_id": execution_id,
            "agent_name": assignment_name,
            "assignment": {
                "assignment_id": assignment_id,
                "agent_name": assignment_name,
                "work_item_id": execution.work_item_id,
                "execution_id": execution_id,
                "event_id": assignment_event.event.event_id,
                "cursor": assignment_event.cursor,
            },
            "work_item": work_item.model_dump(mode="json"),
            "receipt": {
                "receipt_id": receipt_id,
                "status": receipt_payload.get("receipt_status"),
                "agent_name": receipt_name,
                "work_item_id": receipt_work_item,
                "execution_id": receipt_execution,
                "event_id": receipt_event.event.event_id,
                "cursor": receipt_event.cursor,
                "provider_session_id": receipt_payload.get("provider_session_id"),
            },
            "timeline": [
                _event_ref(assignment_event),
                _event_ref(receipt_event),
            ],
            "verified": True,
        }

    @staticmethod
    def trace_reply(
        records: Iterable[Mapping[str, object]],
        draft_id: str,
        *,
        kernel: Kernel | None = None,
    ) -> dict[str, object]:
        """Validate draft → owner approval → bridge send-record provenance."""

        draft_id = validate_id(draft_id)
        indexed: dict[str, Mapping[str, object]] = {}
        for record in records:
            if not isinstance(record, Mapping):
                raise InvariantViolation("supplemental audit records must be mappings")
            record_key = _record_id(record, context="supplemental audit record")
            if record_key in indexed:
                raise InvariantViolation(
                    f"duplicate supplemental audit record: {record_key}"
                )
            indexed[record_key] = record
        draft = indexed.get(draft_id)
        if draft is None:
            raise InvariantViolation(f"reply draft not found: {draft_id}")
        if _kind(draft, context="reply draft") != REPLY_DRAFT_KIND:
            raise InvariantViolation("trace subject is not a reply-draft@1 record")

        created_by = _required_string(
            draft.get("created_by"), field="created_by", context="reply draft"
        )
        if not created_by.startswith("agent:"):
            raise InvariantViolation("reply draft created_by must identify an agent")
        agent_name = created_by.removeprefix("agent:")
        if not agent_name:
            raise InvariantViolation("reply draft created_by has no agent name")
        lineage = _required_string(
            draft.get("lineage"), field="lineage", context="reply draft"
        )
        match = _LINEAGE.fullmatch(lineage)
        if match is None or match.group("agent_name") != agent_name:
            raise InvariantViolation("reply draft lineage does not match created_by")
        work_item_id = validate_id(match.group("work_item_id"))
        execution_id = validate_id(match.group("execution_id"))

        derived_from = draft.get("derived_from")
        if not isinstance(derived_from, Mapping):
            raise InvariantViolation("reply draft derived_from is malformed")
        observations = derived_from.get("observations")
        if not isinstance(observations, list) or len(observations) != 1:
            raise InvariantViolation(
                "reply draft must anchor exactly one incoming observation"
            )
        incoming_observation_id = _required_string(
            observations[0],
            field="incoming observation id",
            context="reply draft",
        )

        payload = draft.get("payload")
        if not isinstance(payload, Mapping):
            raise InvariantViolation("reply draft payload is malformed")
        channel = _required_string(payload.get("channel"), field="channel", context="reply draft")

        approval_candidates = [
            record
            for record in indexed.values()
            if _kind(record, context="supplemental audit record") == REPLY_APPROVAL_KIND
            and draft_id in _derived_supplementals(record, context="reply approval")
        ]
        if len(approval_candidates) != 1:
            raise InvariantViolation(
                "reply draft audit trace requires exactly one anchored approval"
            )
        approval = approval_candidates[0]
        approval_id = _record_id(approval, context="reply approval")

        send_candidates = [
            record
            for record in indexed.values()
            if _kind(record, context="supplemental audit record") == SEND_RECORD_KIND
            and draft_id in _derived_supplementals(record, context="send record")
        ]
        if len(send_candidates) != 1:
            raise InvariantViolation(
                "reply draft audit trace requires exactly one anchored send record"
            )
        send_record = send_candidates[0]
        send_record_id = _record_id(send_record, context="send record")
        send_payload = send_record.get("payload")
        if not isinstance(send_payload, Mapping):
            raise InvariantViolation("send record payload is malformed")
        if send_payload.get("approval_id") != approval_id:
            raise InvariantViolation("send record approval does not match draft approval")
        if send_payload.get("mode") != "approved":
            raise InvariantViolation("send record was not produced by approved delivery")
        if send_payload.get("channel") != channel:
            raise InvariantViolation("send record channel does not match reply draft")

        execution_attribution: dict[str, object] | None = None
        if kernel is not None:
            execution = kernel.executions.get(execution_id)
            work_item = kernel.work_items.get(work_item_id)
            if execution is None or work_item is None:
                raise InvariantViolation(
                    "reply draft lineage references an unknown WorkItem or Execution"
                )
            if execution.work_item_id != work_item_id:
                raise InvariantViolation(
                    "reply draft lineage WorkItem and Execution do not match"
                )
            if execution.agent_name != agent_name:
                raise InvariantViolation(
                    "reply draft agent name does not match Execution attribution"
                )
            execution_attribution = AuditTraceService(kernel).trace_execution(execution_id)

        return {
            "trace_kind": "reply_delivery",
            "draft": {
                "id": draft_id,
                "kind": REPLY_DRAFT_KIND,
                "incoming_observation_id": incoming_observation_id,
                "channel": channel,
                "agent_name": agent_name,
                "work_item_id": work_item_id,
                "execution_id": execution_id,
            },
            "approval": {
                "id": approval_id,
                "kind": REPLY_APPROVAL_KIND,
            },
            "delivery": {
                "id": send_record_id,
                "kind": SEND_RECORD_KIND,
                "approval_id": approval_id,
                "message_ref": send_payload.get("message_ref"),
            },
            "execution_attribution": execution_attribution,
            "verified": True,
        }


def trace_notification_delivery(kernel: Kernel, notification_id: str) -> dict[str, object]:
    return AuditTraceService(kernel).trace_notification(notification_id)


def trace_execution_attribution(kernel: Kernel, execution_id: str) -> dict[str, object]:
    return AuditTraceService(kernel).trace_execution(execution_id)


def trace_reply_chain(
    records: Iterable[Mapping[str, object]],
    draft_id: str,
    *,
    kernel: Kernel | None = None,
) -> dict[str, object]:
    return AuditTraceService.trace_reply(records, draft_id, kernel=kernel)


__all__ = [
    "AuditTraceService",
    "REPLY_APPROVAL_KIND",
    "REPLY_DRAFT_KIND",
    "SEND_RECORD_KIND",
    "trace_execution_attribution",
    "trace_notification_delivery",
    "trace_reply_chain",
]
