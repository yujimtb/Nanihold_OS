"""Property 15 (Concurrent dispatch resilience). Validates Requirements: 6.4, 6.5.

Property statement (design.md §Correctness Properties §Property 15)
-------------------------------------------------------------------
For any ``PolicyDecision`` ``d`` and any subset ``F ⊆ {S3, S4}`` of injected
dispatch failures, ``S5_Policy.dispatch_decision(d)`` SHALL emit *exactly*
``|F|`` ``dispatch_error`` events (each within 1 s of the failure) and SHALL
deliver ``d`` successfully to every recipient in ``{S3, S4} \\ F``, with both
successful deliveries (if any) completing within 1 s of decision production.
Failure of one recipient SHALL NOT prevent dispatch attempt or event emission
for the other.

**Validates: Requirements 6.4, 6.5.**

Test strategy
-------------
The failure-subset universe ``2^{S3,S4}`` has exactly four members
(``{}``, ``{S3}``, ``{S4}``, ``{S3,S4}``), so we enumerate them explicitly
via :func:`pytest.mark.parametrize` rather than via Hypothesis ``@given``.
This is the same idiom used by :mod:`tests.property.test_retry_semantics` for
the discrete ``transient_failures ∈ {0,1,2,3}`` parameter and is equivalent to
``@given(st.sampled_from([...]))`` while keeping the test deterministic and
fast enough for the project-wide ``@settings(max_examples=100)`` budget.

For each subset:

1. Boot a real :class:`Platform` with a deterministic
   :class:`FakeLLMProvider` so that S5's Sub_Agent produces a non-empty
   directive on the first cycle.
2. **Cancel S5's ``run()`` loop** so the test exercises a single, isolated
   :meth:`_handle_assessment` invocation without the platform's natural
   S5→S4→S5 follow-up feedback loop firing additional dispatches. Without
   this isolation, the S4_Scanner's follow-up reply (REQ 5.7) would be
   received by S5's run loop and trigger another dispatch cycle, which
   makes the "exactly ``|F|`` dispatch_error events" invariant of
   Property 15 impossible to assert against the file. The other Systems'
   run loops remain active so the bus / writer behave realistically.
3. Wrap ``s5._bus.send`` with a selective fail-injection shim that returns
   :meth:`SendResult.rejected` for sends originating from S5 on the
   recipients listed in ``F``, and delegates to the original ``send`` for
   every other route. The shim discriminates by ``msg.sender_role`` so that
   no traffic from other Systems is affected.
4. Inject a synthetic :class:`Message` carrying an ``EnvironmentAssessment``
   into S5 by calling :meth:`S5Policy._handle_assessment` directly. This
   bypasses the inbound channel queue (which is irrelevant to the property)
   and exercises the dispatch path in the test coroutine, so all
   ``dispatch_error`` / ``channel_message`` Event_Log appends are scheduled
   on the writer before the test asserts.
5. Assert REQ 6.4 and REQ 6.5 invariants:
   - exactly ``|F|`` ``dispatch_error`` events (REQ 6.5);
   - exactly ``2 - |F|`` ``channel_message`` events whose ``sender`` is S5
     (REQ 6.4: deliveries to recipients in ``{S3, S4} \\ F`` succeed);
   - each ``dispatch_error`` payload references one of the failed
     recipients (REQ 6.5: the event identifies the failed recipient);
   - ``policy_decision`` is appended regardless of dispatch outcomes
     (REQ 6.6: production of the decision is decoupled from dispatch).
"""

from __future__ import annotations

import asyncio

import pytest

from vsm.clock import SystemClock
from vsm.config import RunConfig
from vsm.eventlog.reader import read_all
from vsm.ids import generate_uuid
from vsm.llm.fake import FakeLLMProvider
from vsm.messaging.channels import ChannelId
from vsm.messaging.message import Message, SendResult
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import start_run


# Discrete enumeration of every subset F ⊆ {S3, S4}. Property 15 quantifies
# universally over this set, and ``|2^{S3,S4}| == 4`` makes parametrize the
# natural strategy: it visits every member exactly once and is equivalent to
# (but cheaper than) ``hypothesis.strategies.sampled_from`` over the same
# four pairs. The expected ``dispatch_error`` count is therefore ``int(fail_s3)
# + int(fail_s4)`` and the expected S5-originated ``channel_message`` count is
# ``2 - (int(fail_s3) + int(fail_s4))``.
_FAILURE_SUBSETS = [
    (False, False),  # F = {}     — both deliveries succeed
    (True, False),   # F = {S3}   — S3 fails, S4 succeeds
    (False, True),   # F = {S4}   — S4 fails, S3 succeeds
    (True, True),    # F = {S3,S4}— both fail
]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_s3,fail_s4", _FAILURE_SUBSETS)
async def test_dispatch_resilience(tmp_path, fail_s3: bool, fail_s4: bool) -> None:
    """**Validates: Requirements 6.4, 6.5** — Property 15 over each F ⊆ {S3, S4}.

    For the given failure subset, S5 must emit exactly ``|F|``
    ``dispatch_error`` events and must successfully deliver to every
    recipient in ``{S3, S4} \\ F``.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    # ------------------------------------------------------------------
    # 1. Boot a real Platform with a deterministic LLM. ``response`` is
    # non-empty so S5's Sub_Agent produces a directive that satisfies the
    # ``DirectiveContent.directive`` ``min_length=1`` schema constraint;
    # ``latency=0.0`` keeps the test deterministic.
    # ------------------------------------------------------------------
    fake_llm = FakeLLMProvider(response="execute the directive", latency=0.0)
    platform = await start_run(
        runs_dir=runs_dir,
        run_config=RunConfig(),
        llm_override=fake_llm,
        clock=SystemClock(),
    )
    s5 = platform.systems[SystemRole.S5_POLICY][0]
    s4 = platform.systems[SystemRole.S4_SCANNER][0]

    # ------------------------------------------------------------------
    # 2. Stop S5's run loop so the test owns the single dispatch cycle
    # under observation. Without this, the platform's natural follow-up
    # feedback (S5 → S4 follow-up → S4's REQ 5.7 updated assessment →
    # S5's run loop → another dispatch) fires repeatedly during the
    # test's drain window and inflates ``dispatch_error`` /
    # ``channel_message`` counts above the per-decision values that
    # Property 15 quantifies over.
    #
    # We use the basic :meth:`System.shutdown` path which cancels the
    # ``run()`` Task and awaits its termination. The other Systems' run
    # loops remain active so the bus / writer / Event_Log behave as
    # they would in production.
    # ------------------------------------------------------------------
    await s5.shutdown()

    # ------------------------------------------------------------------
    # 3. Wrap ``s5._bus.send`` with a selective fail-injection shim. We
    # capture the genuine ``send`` *before* the patch so the wrapper can
    # forward to it on the success branch. The shim discriminates by
    # ``msg.sender_role`` and ``msg.channel`` so only S5's outbound
    # dispatches are affected — every other send (e.g. the ``s4_assessment_
    # produced`` follow-up flow, or the implicit S3 / S4 startup sends)
    # delegates to the genuine bus, preserving normal Run behaviour.
    # ------------------------------------------------------------------
    original_send = s5._bus.send

    async def selective_fail(msg: Message) -> SendResult:
        # Only S5's outbound dispatches are subject to fail injection.
        # Inbound messages addressed *to* S5 never traverse ``send`` from
        # an S5_POLICY sender, so the role check is sufficient to scope
        # the patch to the property under test.
        if msg.sender_role == SystemRole.S5_POLICY:
            if msg.channel == ChannelId.S3_S5 and fail_s3:
                # REQ 2.7 / 6.5: the bus may surface a non-throwing
                # rejection. S5's dispatch path treats ``delivered=False``
                # as a failed dispatch and appends ``dispatch_error``.
                return SendResult.rejected(msg.channel)
            if msg.channel == ChannelId.S4_S5 and fail_s4:
                return SendResult.rejected(msg.channel)
        return await original_send(msg)

    # ``s5._bus`` is the singleton :class:`MessageBus` shared by every
    # System; assigning ``send`` on it patches every caller's view of the
    # method. The role / channel guards above keep the patch's blast
    # radius limited to S5's outbound dispatches.
    s5._bus.send = selective_fail  # type: ignore[method-assign]

    # ------------------------------------------------------------------
    # 4. Inject a synthetic ``EnvironmentAssessment`` directly into S5's
    # assessment handler. This bypasses the inbound queue (whose only
    # responsibility is to deliver ``msg`` to ``_handle_assessment``) and
    # exercises the dispatch path in the test's coroutine, so all
    # ``dispatch_error`` / ``channel_message`` Event_Log appends complete
    # before we assert.
    # ------------------------------------------------------------------
    assessment_msg = Message(
        message_id=generate_uuid(),
        sender_role=SystemRole.S4_SCANNER,
        sender_id=s4.system_id,
        receiver_role=SystemRole.S5_POLICY,
        receiver_id=s5.system_id,
        channel=ChannelId.S4_S5,
        payload={
            "assessment_id": generate_uuid(),
            "opportunities": ["opp1"],
            "threats": ["threat1"],
        },
        timestamp_ms=0,
    )

    try:
        await s5._handle_assessment(assessment_msg)
        # Allow the writer task to drain its queue. The dispatch /
        # ``policy_decision`` / ``dispatch_error`` appends are all
        # ``await``-ed inside ``_handle_assessment``, so by the time the
        # call returns they are already enqueued on the writer; this
        # short sleep lets the writer task flush them to ``events.jsonl``.
        await asyncio.sleep(0.5)

        events_path = platform.run_dir / "events.jsonl"
        events = read_all(events_path)

        # ------------------------------------------------------------------
        # 4a. REQ 6.5: exactly ``|F|`` ``dispatch_error`` events.
        # ------------------------------------------------------------------
        # We restrict to events that reference S5's recipients (S3 / S4)
        # so any unrelated dispatch errors elsewhere in the platform do
        # not skew the count. The S5 implementation emits ``recipient``
        # values "S3_ALLOCATOR" / "S4_SCANNER".
        dispatch_errors = [
            e
            for e in events
            if e["event_type"] == "dispatch_error"
            and e["payload"].get("recipient")
            in ("S3_ALLOCATOR", "S4_SCANNER")
        ]
        expected_errors = int(fail_s3) + int(fail_s4)
        assert len(dispatch_errors) == expected_errors, (
            f"REQ 6.5: expected exactly {expected_errors} dispatch_error "
            f"events for F={{S3 if fail_s3 else None, S4 if fail_s4 else None}} "
            f"(fail_s3={fail_s3}, fail_s4={fail_s4}); got {len(dispatch_errors)}: "
            f"{[e['payload'] for e in dispatch_errors]}"
        )

        # Every emitted ``dispatch_error`` must reference a *failed*
        # recipient — the bus shim never rejects a recipient that is
        # absent from F, so the converse never happens for an honest
        # implementation.
        failed_recipients: set[str] = set()
        if fail_s3:
            failed_recipients.add("S3_ALLOCATOR")
        if fail_s4:
            failed_recipients.add("S4_SCANNER")
        for evt in dispatch_errors:
            recipient = evt["payload"].get("recipient")
            assert recipient in failed_recipients, (
                f"REQ 6.5: dispatch_error references recipient {recipient!r} "
                f"which is not in the injected failure set {failed_recipients}; "
                f"payload={evt['payload']}"
            )

        # ------------------------------------------------------------------
        # 4b. REQ 6.4: deliveries to recipients in {S3, S4} \\ F succeed.
        # ------------------------------------------------------------------
        # S5 emits a ``channel_message`` Event_Log entry per *successful*
        # ``MessageBus.send`` (the bus appends it on the delivery path).
        # The shim short-circuits the bus for failed recipients, so the
        # number of ``channel_message`` events whose ``sender`` is S5 is
        # exactly ``2 - |F|``.
        s5_channel_messages = [
            e
            for e in events
            if e["event_type"] == "channel_message"
            and e["payload"].get("sender") == s5.system_id
        ]
        expected_successes = 2 - expected_errors
        assert len(s5_channel_messages) == expected_successes, (
            f"REQ 6.4: expected exactly {expected_successes} S5-originated "
            f"channel_message events (deliveries to {{S3,S4}} \\ F) for "
            f"fail_s3={fail_s3}, fail_s4={fail_s4}; got "
            f"{len(s5_channel_messages)}: "
            f"{[e['payload'].get('channel') for e in s5_channel_messages]}"
        )

        # Every successful delivery must be on one of the two expected
        # channels and must address a recipient that is *not* in F.
        # ``S3-S5`` reaches S3_Allocator; ``S4-S5`` reaches S4_Scanner.
        succeeded_channels = {
            e["payload"].get("channel") for e in s5_channel_messages
        }
        if not fail_s3:
            assert ChannelId.S3_S5.value in succeeded_channels, (
                f"REQ 6.4: S3 not in F (fail_s3=False) but no S5 → S3 "
                f"channel_message observed; got channels={succeeded_channels}"
            )
        if not fail_s4:
            assert ChannelId.S4_S5.value in succeeded_channels, (
                f"REQ 6.4: S4 not in F (fail_s4=False) but no S5 → S4 "
                f"channel_message observed; got channels={succeeded_channels}"
            )
        if fail_s3:
            assert ChannelId.S3_S5.value not in succeeded_channels, (
                f"REQ 6.5: S3 in F (fail_s3=True) but an S5 → S3 "
                f"channel_message was observed; got channels={succeeded_channels}"
            )
        if fail_s4:
            assert ChannelId.S4_S5.value not in succeeded_channels, (
                f"REQ 6.5: S4 in F (fail_s4=True) but an S5 → S4 "
                f"channel_message was observed; got channels={succeeded_channels}"
            )

        # ------------------------------------------------------------------
        # 4c. REQ 6.6: ``policy_decision`` is emitted regardless of
        # dispatch outcomes. Failure of one (or both) dispatch(es) must
        # not block the production / observation of the decision itself.
        # ------------------------------------------------------------------
        policy_decisions = [
            e for e in events if e["event_type"] == "policy_decision"
        ]
        assert len(policy_decisions) >= 1, (
            "REQ 6.6: at least one policy_decision must be emitted even "
            "when both dispatches fail; got "
            f"{[e['event_type'] for e in events]}"
        )
    finally:
        await platform.shutdown()
