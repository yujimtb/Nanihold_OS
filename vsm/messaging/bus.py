"""Self-implemented :class:`MessageBus` for the VSM PoC platform.

The bus is the only authority allowed to deliver messages between Systems
(REQ 2.1〜2.6) and is the only authority that records ``channel_message`` and
``channel_rejected`` entries in the Event_Log (REQ 2.7〜2.9). It runs on the
single asyncio event loop that hosts every System, so all routing, queue
placement, and Event_Log append hand-off complete in the same loop tick — a
constraint that is necessary to satisfy the REQ 2.9 1-second delivery SLA
without adding cross-thread synchronisation.

Design notes
------------
* **Subscription map keyed by ``(receiver_system_id, channel)``.** A System
  binds its own queue at start-up via :meth:`subscribe`. The bus only ever
  enqueues into the queue addressed by the *outgoing message's*
  ``receiver_id`` and ``channel`` fields. This is the structural guarantee
  for REQ 9.1: an ``S3STAR_TO_S1`` message can only land in the queue that
  some S1_Worker has subscribed for that exact ``(s1_id, S3STAR_TO_S1)`` key.
  S3_Allocator could not subscribe to ``S3STAR_TO_S1`` even if it tried — the
  static :data:`ALLOWED_ROUTES` table does not list it as a receiver — and
  even a hypothetical buggy subscription on S3_Allocator's side would never
  receive a message whose ``receiver_id`` is an S1's id. The S3* → S1 path
  therefore bypasses S3_Allocator by construction, not by convention.
* **Rejection is non-exceptional.** REQ 2.7 says the bus "SHALL return a
  rejection indication". We model that as :meth:`SendResult.rejected` rather
  than raising :class:`ChannelRejected`, so the caller can branch on routing
  results in business logic without wrapping every send in ``try/except``.
* **Two rejection cases.** Routes outside :data:`ALLOWED_ROUTES` are the
  primary REQ 2.7 case. We additionally treat "the receiver has not
  subscribed yet" as a rejection so that an out-of-order start-up does not
  silently drop messages. Both surface as a ``channel_rejected`` Event_Log
  entry plus :meth:`SendResult.rejected` — replay can still distinguish the
  two cases by inspecting the receiver_id in the payload.
* **Single event-loop tick delivery.** ``send`` performs an O(1) frozen-set
  membership check, an O(1) dict lookup, and a non-blocking
  :meth:`asyncio.Queue.put_nowait`. The Event_Log ``append`` is itself a
  hand-off into another asyncio queue, so the whole path is dominated by
  in-memory operations. The 1-second SLA in REQ 2.9 is met by structure, not
  by tuning.

Validates Requirements
----------------------
- REQ 2.1〜2.6: routes defined in :data:`ALLOWED_ROUTES` deliver into the
  receiver's subscription queue.
- REQ 2.7: routes outside :data:`ALLOWED_ROUTES` are rejected and the
  rejection indication identifies the offending channel.
- REQ 2.8: rejections are recorded with sender / receiver / channel and a
  millisecond-precision timestamp (the timestamp lives on the Event_Log
  envelope ``ts`` field set by :class:`EventLogWriter`).
- REQ 2.9: deliveries are recorded with sender / receiver / channel /
  payload and a millisecond-precision timestamp.
- REQ 9.1: S3* → S1 messages bypass S3_Allocator (structural guarantee via
  the subscription-map key).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from vsm.eventlog.writer import EventLogWriter
from vsm.messaging.channels import ChannelId, is_allowed
from vsm.messaging.message import Message, SendResult

__all__ = ["MessageBus"]


class MessageBus:
    """Single-process asyncio message bus for the VSM PoC platform.

    The bus owns:

    * the static route allow-list :data:`vsm.messaging.channels.ALLOWED_ROUTES`
      (consulted via :func:`vsm.messaging.channels.is_allowed`); and
    * a private subscription map of ``(receiver_id, channel) ->
      asyncio.Queue[Message]`` populated by :meth:`subscribe` at System
      start-up.

    All deliveries and all rejections are recorded in the Event_Log via the
    injected :class:`EventLogWriter` so that replay can reconstruct the
    exact channel-event history (REQ 10.10).

    Parameters
    ----------
    eventlog : EventLogWriter
        The single Event_Log writer for the active Run. The bus does not
        own its lifecycle — the Run lifecycle layer starts and stops it —
        but the bus depends on it being already running before
        :meth:`send` is awaited.

    Validates Requirements
    ----------------------
    REQ 2.1〜2.9, 9.1.
    """

    def __init__(self, eventlog: EventLogWriter) -> None:
        self._eventlog: EventLogWriter = eventlog
        # Subscription map. Keyed by ``(receiver_id, channel)``: the only
        # combination the bus uses when routing a message is exactly
        # ``(msg.receiver_id, msg.channel)``, so this key shape is what
        # makes the S3* → S1 isolation property structural rather than
        # conventional. ``asyncio.Queue`` is unbounded by default, which
        # matches the PoC scale (REQ 1.3 caps S1 count at 1024 and
        # representative scenarios produce O(10²) messages).
        self._queues: dict[tuple[str, ChannelId], asyncio.Queue[Message]] = {}
        self._suspended_receivers: set[str] = set()
        self._pending: dict[str, list[Message]] = {}
        self._pending_ids: set[str] = set()
        self._on_deferred: Callable[[], None] | None = None

    def set_deferred_callback(self, callback: Callable[[], None] | None) -> None:
        self._on_deferred = callback

    def suspend_receiver(self, receiver_id: str) -> None:
        self._suspended_receivers.add(receiver_id)

    def defer(self, msg: Message) -> None:
        if msg.message_id in self._pending_ids:
            return
        self._pending.setdefault(msg.receiver_id, []).append(msg)
        self._pending_ids.add(msg.message_id)
        if self._on_deferred is not None:
            self._on_deferred()

    def resume_receiver(self, receiver_id: str) -> int:
        self._suspended_receivers.discard(receiver_id)
        pending = self._pending.pop(receiver_id, [])
        for msg in pending:
            queue = self._queues.get((receiver_id, msg.channel))
            if queue is None:
                raise RuntimeError(
                    f"pending message receiver is not subscribed: {receiver_id}/{msg.channel.value}"
                )
            queue.put_nowait(msg)
            self._pending_ids.discard(msg.message_id)
        return len(pending)

    def deferred_messages(self) -> list[Message]:
        """返却する保留 Message のスナップショット。"""

        return [message for messages in self._pending.values() for message in messages]

    def restore_deferred(self, messages: list[Message]) -> None:
        """durable quota state から保留 Message を復元する。"""

        for message in messages:
            if message.message_id in self._pending_ids:
                continue
            self._pending.setdefault(message.receiver_id, []).append(message)
            self._pending_ids.add(message.message_id)

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(
        self, system_id: str, channel: ChannelId
    ) -> asyncio.Queue[Message]:
        """Register a System for messages on a channel.

        Each ``(system_id, channel)`` pair maps to exactly one queue: a
        repeated subscription returns the existing queue rather than
        creating a fresh one, so that a System restarting its receive loop
        does not accidentally orphan in-flight messages.

        Parameters
        ----------
        system_id : str
            Identifier of the receiving System instance. The bus uses this
            value verbatim when routing — it is the caller's responsibility
            to use the same id on outgoing :attr:`Message.receiver_id`
            fields.
        channel : ChannelId
            The channel on which the System wants to receive.

        Returns
        -------
        asyncio.Queue[Message]
            The queue that will receive every :class:`Message` whose
            ``(receiver_id, channel)`` matches this subscription.

        Notes
        -----
        Subscription does **not** validate against :data:`ALLOWED_ROUTES`:
        a System may subscribe to any channel, but the bus will only ever
        enqueue into a subscription whose route is allowed (because
        :meth:`send` rejects disallowed routes before it touches any queue).
        This keeps the subscription side cheap and avoids accidental
        coupling between subscriber knowledge and the route table.

        Validates Requirements
        ----------------------
        REQ 2.1〜2.6, 9.1.
        """
        key = (system_id, channel)
        queue = self._queues.get(key)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[key] = queue
        return queue

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    async def send(self, msg: Message) -> SendResult:
        """Route ``msg`` and append the corresponding Event_Log entry.

        The full routing path, in order, is:

        1. **Static route check** against
           :func:`vsm.messaging.channels.is_allowed`. If the
           ``(sender_role, receiver_role, channel)`` triple is not in the
           allow-list, the bus appends a ``channel_rejected`` event and
           returns :meth:`SendResult.rejected` — REQ 2.7 / 2.8.
        2. **Subscription lookup** via ``self._queues[(receiver_id,
           channel)]``. A missing subscription is treated as a rejection
           (still ``channel_rejected``); REQ 2.7 only mandates rejection for
           undefined channels but silently dropping a message because the
           receiver started slowly would violate the spirit of REQ 2.9, so
           we surface the failure explicitly.
        3. **Non-blocking enqueue** via :meth:`asyncio.Queue.put_nowait`.
           The default unbounded queue cannot raise :class:`asyncio.QueueFull`
           in normal operation; if it ever did, the exception would
           propagate to the caller as a hard failure rather than being
           swallowed, because back-pressure surprises in a PoC are worse
           than crashes.
        4. **Event_Log append** of a ``channel_message`` entry, carrying
           sender / receiver / channel / payload — REQ 2.9.

        Steps 1–3 complete in the same event-loop tick (only in-memory
        operations); step 4's hand-off into the writer queue is also a
        sub-millisecond operation. The 1-second delivery SLA in REQ 2.9 is
        therefore satisfied by structure.

        Parameters
        ----------
        msg : Message
            The message to route. ``msg.payload`` MUST NOT be mutated by
            the caller after this method is awaited; the dict reference is
            shared with the Event_Log writer, and concurrent mutation
            would corrupt the persisted record.

        Returns
        -------
        SendResult
            ``SendResult.ok()`` on successful enqueue;
            ``SendResult.rejected(msg.channel)`` on either a disallowed
            route or an unsubscribed receiver.

        Validates Requirements
        ----------------------
        REQ 2.1〜2.9, 9.1.
        """
        # Step 1 — REQ 2.7: static route check. ``is_allowed`` is an O(1)
        # frozen-set membership test, so this branch is essentially free.
        if not is_allowed(msg.sender_role, msg.receiver_role, msg.channel):
            # REQ 2.8: record the rejection. The Event_Log envelope's
            # ``ts`` field, set by EventLogWriter.append from the injected
            # clock, supplies the millisecond-precision timestamp.
            await self._eventlog.append(
                "channel_rejected",
                {
                    "sender": msg.sender_id,
                    "receiver": msg.receiver_id,
                    "channel": msg.channel.value,
                },
            )
            # REQ 2.7: the rejection indication identifies the rejected
            # channel by carrying it in SendResult.rejected_channel.
            return SendResult.rejected(msg.channel)

        # Step 2 — subscription lookup. The key shape ``(receiver_id,
        # channel)`` is the structural barrier that keeps S3STAR_TO_S1
        # messages out of S3_Allocator's queues (REQ 9.1): regardless of
        # what S3_Allocator subscribes to, a message whose receiver_id is
        # an S1's id and whose channel is S3STAR_TO_S1 will only ever land
        # in that S1's queue. Note that the static route check above
        # already guarantees that ``(S3STAR_AUDITOR, S1_WORKER,
        # S3STAR_TO_S1)`` is allowed and that no symmetrical
        # ``(S3STAR_AUDITOR, S3_ALLOCATOR, S3STAR_TO_S1)`` route exists.
        key = (msg.receiver_id, msg.channel)
        queue = self._queues.get(key)
        if queue is None:
            # The route is allowed but the receiver hasn't bound a queue
            # yet. Treat as rejection so the caller can react rather than
            # silently lose the message. Logged with the same
            # ``channel_rejected`` event_type so replay can identify both
            # rejection causes from the same record class.
            await self._eventlog.append(
                "channel_rejected",
                {
                    "sender": msg.sender_id,
                    "receiver": msg.receiver_id,
                    "channel": msg.channel.value,
                },
            )
            return SendResult.rejected(msg.channel)

        # Step 3 — enqueue without yielding the loop. ``put_nowait``
        # avoids creating a task scheduler hop and surfaces back-pressure
        # immediately if a future bounded-queue policy is introduced.
        if msg.receiver_id in self._suspended_receivers:
            self.defer(msg)
        else:
            queue.put_nowait(msg)

        # Step 4 — REQ 2.9: append the delivery record. ``payload`` is the
        # System-defined dict; the Event_Log writer validates it against
        # the ``channel_message`` pydantic model before persisting.
        await self._eventlog.append(
            "channel_message",
            {
                "sender": msg.sender_id,
                "receiver": msg.receiver_id,
                "channel": msg.channel.value,
                "payload": msg.payload,
            },
        )
        return SendResult.ok()
