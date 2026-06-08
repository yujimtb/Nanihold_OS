# Feature: Nanihold OS, Properties 1 and 2: Channel rejection and delivery invariants
"""Property-based tests for :class:`vsm.messaging.bus.MessageBus`.

This module implements two properties from design.md §Correctness Properties
that together pin down the entire static-route behaviour of the
Message_Bus:

* **Property 1: Channel rejection invariant** — for every
  ``(sender_role, receiver_role, channel)`` triple **not** present in
  :data:`vsm.messaging.channels.ALLOWED_ROUTES`, :meth:`MessageBus.send`
  MUST return ``SendResult(delivered=False, rejected_channel=channel)``,
  MUST NOT enqueue anything into the receiver's subscription queue, and
  MUST append exactly one ``channel_rejected`` event identifying the
  sender, receiver, and channel.

  **Validates: Requirements 2.7, 2.8**

* **Property 2: Channel delivery invariant** — for every triple **in**
  :data:`ALLOWED_ROUTES`, :meth:`MessageBus.send` MUST return
  ``SendResult(delivered=True)``, the receiver's subscription queue MUST
  contain exactly the message that was sent, and the bus MUST append
  exactly one ``channel_message`` event whose payload echoes the
  sender / receiver / channel triple.

  **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.9**

Strategy
--------
A single :func:`hypothesis.given` test draws ``(sender, receiver, channel)``
from ``SystemRole × SystemRole × ChannelId`` and dispatches to the
allow-list branch or the reject branch based on
``(sender, receiver, channel) in ALLOWED_ROUTES``. Folding both
properties into one test maximises Hypothesis coverage of the joint
input space (36 × 7 = 252 triples, of which only 12 are allowed) and
guarantees that the negative space is exercised on every run rather than
relying on Hypothesis to stumble onto disallowed combinations.

The Hypothesis configuration follows the project-wide convention
recorded in tasks.md: ``@settings(max_examples=100, deadline=None)``.
``deadline=None`` is required because each example performs real
asyncio I/O (creating a tmp Run directory, starting the EventLogWriter
task, sleeping for the writer-drain budget, stopping the task, reading
the JSONL file). Hypothesis' default per-example deadline does not
account for that fixed overhead.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from hypothesis import given, settings, strategies as st

from vsm.clock import SystemClock
from vsm.eventlog.writer import EventLogWriter
from vsm.ids import generate_uuid
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ALLOWED_ROUTES, ChannelId
from vsm.messaging.message import Message
from vsm.roles import SystemRole


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# REQ 1.1: ``SystemRole`` enumerates the six VSM roles. Drawing senders and
# receivers independently from this enum produces 36 ordered pairs, which
# combined with the 7-member ``ChannelId`` enum yields 252 triples — only
# 12 of which appear in :data:`ALLOWED_ROUTES`. This skew is intentional:
# the rejection branch is exercised on roughly 95% of examples on each
# run, which is exactly what we want to validate REQ 2.7.
_role_strategy = st.sampled_from(list(SystemRole))
_channel_strategy = st.sampled_from(list(ChannelId))


# ---------------------------------------------------------------------------
# Joint property: rejection (Property 1) ∪ delivery (Property 2)
# ---------------------------------------------------------------------------


@given(
    sender=_role_strategy,
    receiver=_role_strategy,
    channel=_channel_strategy,
)
@settings(max_examples=100, deadline=None)
def test_channel_invariant(tmp_path_factory, sender, receiver, channel):
    """Properties 1 + 2: every ``(sender, receiver, channel)`` triple either
    delivers or rejects, deterministically, with a matching Event_Log entry.

    Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9.

    The body wraps an inner async coroutine in :func:`asyncio.run` so that
    each Hypothesis example runs in its own fresh event loop — Hypothesis
    invokes the test function as a plain sync callable, so we cannot rely
    on pytest-asyncio's per-test loop here. A fresh loop per example also
    isolates the :class:`EventLogWriter` task lifecycle and keeps the
    ``asyncio.Queue`` cleanup deterministic.
    """

    # ``tmp_path_factory`` is session-scoped, so it is safe to call across
    # the 100 Hypothesis examples; each ``mktemp`` call returns a unique
    # subdirectory. We create the run directory inside the tmp tree by
    # hand so that the EventLogWriter (which expects the parent directory
    # to already exist, REQ 10.3) can simply append to the file.
    run_dir = tmp_path_factory.mktemp("bus")
    events_path = run_dir / "events.jsonl"

    is_allowed_route = (sender, receiver, channel) in ALLOWED_ROUTES

    async def _scenario() -> None:
        writer = EventLogWriter(
            run_id="run-bus", path=events_path, clock=SystemClock()
        )
        await writer.start()
        bus = MessageBus(eventlog=writer)

        # Distinct sender / receiver ids so the rejected-event payload
        # cannot accidentally match a delivered-event payload via id
        # collision.
        sender_id = generate_uuid()
        receiver_id = generate_uuid()

        # Subscribe the receiver on the channel under test. Doing this
        # unconditionally (i.e. even for disallowed routes) means that
        # the only reason a disallowed-route message can be rejected is
        # the static :func:`is_allowed` check — not a missing
        # subscription. This keeps Property 1 isolated from the
        # "subscription absent" rejection branch handled separately by
        # the bus.
        queue = bus.subscribe(receiver_id, channel)

        msg = Message(
            message_id=generate_uuid(),
            sender_role=sender,
            sender_id=sender_id,
            receiver_role=receiver,
            receiver_id=receiver_id,
            channel=channel,
            payload={"k": "v"},
            timestamp_ms=1234567890123,
        )

        result = await bus.send(msg)

        # Allow the EventLogWriter task to drain its queue and fsync the
        # line(s) for this example. 200 ms is comfortably above the
        # 100 ms append-visibility SLA (REQ 10.5) so the read-back below
        # is race-free, while still keeping the 100-example test under a
        # minute of wall-clock.
        await asyncio.sleep(0.2)
        await writer.stop()

        events = [
            json.loads(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]

        if is_allowed_route:
            # Property 2: delivery invariant (REQ 2.1〜2.6, 2.9).
            assert result.delivered is True
            assert result.rejected_channel is None

            # The receiver queue MUST contain exactly the one message we
            # sent — no spurious duplication, no off-by-one. Reading
            # from the queue here is purely for assertion; the bus does
            # not require it.
            assert queue.qsize() == 1
            delivered_msg = queue.get_nowait()
            assert delivered_msg.message_id == msg.message_id

            # Exactly one ``channel_message`` event with the right
            # sender / receiver / channel triple. The bus emits no other
            # events on the success path, so a count of 1 is the strict
            # invariant.
            delivery_events = [
                e for e in events if e["event_type"] == "channel_message"
            ]
            assert len(delivery_events) == 1
            de = delivery_events[0]
            assert de["payload"]["sender"] == sender_id
            assert de["payload"]["receiver"] == receiver_id
            assert de["payload"]["channel"] == channel.value
            assert de["payload"]["payload"] == {"k": "v"}

            # And no ``channel_rejected`` event leaks into the success
            # path (would indicate a routing-decision bug).
            assert not any(
                e["event_type"] == "channel_rejected" for e in events
            )
        else:
            # Property 1: rejection invariant (REQ 2.7, 2.8).
            assert result.delivered is False
            # REQ 2.7: the rejection indication MUST identify the
            # rejected channel.
            assert result.rejected_channel == channel

            # No message reaches the receiver's queue.
            assert queue.qsize() == 0

            # Exactly one ``channel_rejected`` event identifying
            # sender / receiver / channel (REQ 2.8). The bus emits no
            # ``channel_message`` event on the reject path.
            rejection_events = [
                e for e in events if e["event_type"] == "channel_rejected"
            ]
            assert len(rejection_events) == 1
            re_ = rejection_events[0]
            assert re_["payload"]["sender"] == sender_id
            assert re_["payload"]["receiver"] == receiver_id
            assert re_["payload"]["channel"] == channel.value

            assert not any(
                e["event_type"] == "channel_message" for e in events
            )

    asyncio.run(_scenario())
