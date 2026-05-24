"""Message envelope and bus send-result types for the VSM Message_Bus.

This module defines the in-memory shape of every message that flows through
:class:`vsm.messaging.bus.MessageBus`, and the non-exceptional result type
that :meth:`MessageBus.send` returns.

The two types are deliberately separated from the bus implementation so that
they can be referenced by:

* the bus itself (for routing / queue placement);
* every :class:`vsm.systems.base.System` subclass (as the wire payload type);
* the Event_Log writer (which serialises ``Message.payload`` into
  ``channel_message`` and ``channel_rejected`` records);
* property-based tests in :mod:`tests.property.test_message_bus`.

Design notes
------------
* Both dataclasses use ``frozen=True, slots=True`` to make them hashable,
  cheap to allocate, and immutable on the *reference* level. ``slots=True``
  also prevents accidental attribute additions.
* :class:`Message` carries the **sender role + id** and **receiver role + id**
  as separate fields rather than as a tuple. This matches design.md
  §Data Models §Channel メッセージスキーマ exactly and keeps Event_Log
  payload construction trivial (one ``asdict`` call yields the right keys).
* :attr:`Message.payload` is intentionally typed as ``dict``: each System
  defines its own per-channel payload schema, so a permissive ``dict`` is
  the only common type. Callers MUST treat the dict as if it were frozen
  after the message is sent — the dataclass freezes the *reference* but the
  ``dict`` itself is still mutable. Mutating the dict after :meth:`send`
  would corrupt the Event_Log record. We document this contract here rather
  than wrapping the dict in :class:`types.MappingProxyType` to keep the PoC
  hot path allocation-free.
* :attr:`Message.timestamp_ms` is an ``int`` of millisecond precision per
  REQ 2.8 / 2.9 (the wall-clock timestamp at which the bus accepted the
  message). The same precision is reflected in the corresponding Event_Log
  ``ts`` field (REQ 10.7).
* :class:`SendResult` encodes REQ 2.7's "rejection indication" as a value
  rather than as an exception. This lets callers branch on rejection in
  business logic (e.g. retrying with an alternate channel) without having
  to wrap every send in a ``try/except``. The dedicated exception
  :class:`vsm.errors.ChannelRejected` still exists for callers that *want*
  to raise (see :mod:`vsm.errors`); ``SendResult`` is the structural,
  non-throwing path used by the bus by default.

References
----------
- REQ 2.7: rejection indication identifies the rejected Channel.
- REQ 2.8: rejection events carry millisecond-precision timestamps.
- REQ 2.9: delivery events carry sender/receiver/channel/payload and a
  millisecond-precision timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass

from vsm.messaging.channels import ChannelId
from vsm.roles import SystemRole

__all__ = ["Message", "SendResult"]


@dataclass(frozen=True, slots=True)
class Message:
    """A single message sent on a :class:`ChannelId`.

    Attributes
    ----------
    message_id:
        UUIDv4 string (use :func:`vsm.ids.generate_uuid`). Must be unique
        within a Run; used as the dedupe key in property-based tests.
    sender_role:
        Role of the sending System (see :class:`SystemRole`).
    sender_id:
        Stable identifier of the sending System instance.
    receiver_role:
        Role of the receiving System.
    receiver_id:
        Stable identifier of the receiving System instance.
    channel:
        The channel the message is being sent on. The bus rejects any
        ``(sender_role, receiver_role, channel)`` triple not present in
        :data:`vsm.messaging.channels.ALLOWED_ROUTES` (REQ 2.7).
    payload:
        Per-System message body. The dict is logically immutable once the
        message is handed to the bus — callers MUST NOT mutate it.
    timestamp_ms:
        Millisecond-precision wall-clock timestamp at which the bus
        accepted the message (REQ 2.8 / 2.9). Stored as an ``int`` of
        milliseconds since the Unix epoch.

    Validates Requirements: 2.8, 2.9 (millisecond precision is encoded by
    the ``timestamp_ms`` field type and unit contract).
    """

    message_id: str
    sender_role: SystemRole
    sender_id: str
    receiver_role: SystemRole
    receiver_id: str
    channel: ChannelId
    payload: dict
    timestamp_ms: int


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outcome of :meth:`vsm.messaging.bus.MessageBus.send`.

    REQ 2.7 requires that rejected channels are surfaced as an
    *indication* that identifies the rejected Channel, not as a fatal
    error. Returning a ``SendResult`` value (rather than raising
    :class:`vsm.errors.ChannelRejected`) lets the caller make the
    routing decision in business logic and keeps the bus side-effect-free
    for the calling coroutine.

    Two construction paths are provided:

    * :meth:`ok` — the message was accepted and queued for the receiver.
    * :meth:`rejected` — the route was not in ``ALLOWED_ROUTES``; the
      ``rejected_channel`` field carries the offending channel so that
      callers and tests can branch on it without re-deriving the value.

    Attributes
    ----------
    delivered:
        ``True`` iff the message was queued for the receiver. ``False``
        only when the route was rejected.
    rejected_channel:
        The :class:`ChannelId` that was rejected, or ``None`` on success.
        Populated by :meth:`rejected` so that REQ 2.7's "identifies the
        rejected Channel" obligation is satisfied without consulting the
        original :class:`Message`.

    Validates Requirements: 2.7.
    """

    delivered: bool
    rejected_channel: ChannelId | None = None

    @classmethod
    def ok(cls) -> "SendResult":
        """Return the canonical success result: ``delivered=True``.

        Used by :meth:`MessageBus.send` after a successful enqueue.
        """
        return cls(delivered=True, rejected_channel=None)

    @classmethod
    def rejected(cls, channel: ChannelId) -> "SendResult":
        """Return a rejection result that identifies ``channel``.

        Used by :meth:`MessageBus.send` when
        :func:`vsm.messaging.channels.is_allowed` returns ``False`` for
        the attempted ``(sender_role, receiver_role, channel)`` triple.

        Args:
            channel: The :class:`ChannelId` whose route was rejected.

        Returns:
            A :class:`SendResult` with ``delivered=False`` and
            ``rejected_channel=channel``.
        """
        return cls(delivered=False, rejected_channel=channel)
