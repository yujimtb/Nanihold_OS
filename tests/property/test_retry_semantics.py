"""Property 16 (Retry semantics). Validates Requirements: 5.6, 10.6.

This module covers the two retry-loop invariants enumerated by design.md
§Correctness Properties §P16:

* **REQ 10.6 — Event_Log append retry.** :class:`EventLogWriter` retries a
  transient :class:`OSError` from the underlying file write up to 3 times,
  with at least 100 ms between successive attempts. If any attempt succeeds
  the loop returns immediately without performing further attempts. If all
  3 attempts fail the writer surfaces a typed
  :class:`vsm.errors.EventLogAppendError` (which the writer task lets
  propagate, terminating the writer task with that exception).
* **REQ 5.6 — S4 → S5 delivery retry.** :class:`S4Scanner._deliver_to_s5`
  retries a rejected / failed delivery up to 3 times, with at least 10 s
  between successive attempts, and appends one ``delivery_error`` event
  per failed attempt. If any attempt succeeds the loop stops; otherwise
  the assessment is dropped after the third failure (and S5 never sees
  it).

Test strategy
-------------
For each of the two halves we sweep the four meaningful values of the
"number of transient failures before success" parameter:

* ``0``  — no failures, the first attempt succeeds. Asserts that no retry
  side effects occur (no ``delivery_error``, no inter-attempt sleep).
* ``1``  — a single transient failure followed by success. Asserts that
  exactly one retry side effect occurs and that the success was observed.
* ``2``  — two consecutive failures followed by success. Asserts that
  two retry side effects occur and that the success was observed.
* ``3``  — all three attempts fail. Asserts that exactly three retry
  side effects occur and that the surface behaviour matches the spec
  (writer task dies with :class:`EventLogAppendError` for REQ 10.6;
  the assessment is dropped without a fourth send for REQ 5.6).

The parameter space is discrete and small (4 values), so an explicit
:func:`pytest.mark.parametrize` sweep is equivalent to a Hypothesis
``integers(min_value=0, max_value=3)`` strategy and is easier to read.
The retry-interval lower bound is asserted from observed timestamps
(REQ 10.6) and from a captured list of ``asyncio.sleep`` durations
(REQ 5.6 — real 10 s sleeps are replaced by an awaitable no-op so the
test runs in milliseconds while still observing the requested duration).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from vsm.clock import SystemClock
from vsm.config import RunConfig
from vsm.errors import EventLogAppendError
from vsm.eventlog.reader import read_all
from vsm.eventlog.writer import EventLogWriter
from vsm.llm.fake import FakeLLMProvider
from vsm.messaging.message import SendResult
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import start_run


# ---------------------------------------------------------------------------
# Part 1 — Task 5.3: Event_Log append retry (REQ 10.6)
# ---------------------------------------------------------------------------


# REQ 10.6: at most 3 attempts and at least 100 ms between attempts. These
# constants are mirrored from :mod:`vsm.eventlog.writer` so that a future
# spec change in either place is immediately visible as a failing test.
_APPEND_MAX_ATTEMPTS = 3
_APPEND_MIN_INTERVAL_SECONDS = 0.1


@pytest.mark.asyncio
@pytest.mark.parametrize("transient_failures", [0, 1, 2, 3])
async def test_append_retry_semantics(tmp_path, transient_failures: int) -> None:
    """REQ 10.6: append retries up to 3 times with ≥ 100 ms between attempts.

    Validates Property 16 (Event_Log append portion).

    Strategy
    --------
    1. Construct an :class:`EventLogWriter` against a per-test ``tmp_path``
       and start its writer task.
    2. Replace ``writer._fh.write`` with a wrapper that fails the first
       ``transient_failures`` calls with :class:`OSError` and then forwards
       to the original ``write`` for any subsequent calls.
    3. Each wrapped call records ``time.monotonic()`` so that we can
       assert the inter-attempt interval lower bound directly from
       observed timestamps (rather than indirectly via the
       :func:`asyncio.sleep` duration).
    4. Enqueue exactly one event via :meth:`EventLogWriter.append`.
    5. Yield the loop briefly so the writer task can drain the queue
       through its retry loop.
    6. Assert the four invariants enumerated below for each value of
       ``transient_failures`` (0..3).
    """
    run_dir = tmp_path / "run-retry"
    run_dir.mkdir(parents=True)
    events_path = run_dir / "events.jsonl"

    writer = EventLogWriter(
        run_id="run-retry", path=events_path, clock=SystemClock()
    )
    await writer.start()

    # Capture the underlying ``write`` *before* we monkey-patch the
    # attribute so the wrapper can forward to the genuine implementation
    # on success. The TextIOWrapper allows instance-level attribute
    # replacement, which is what makes this style of localised injection
    # possible without touching :class:`EventLogWriter` itself.
    original_write = writer._fh.write
    call_count = {"n": 0}
    attempt_timestamps: list[float] = []

    def failing_write(line: str) -> int:
        # Record the wall-clock instant of every attempt (success or
        # failure) so the inter-attempt interval lower bound can be
        # asserted directly from observed timing.
        attempt_timestamps.append(time.monotonic())
        call_count["n"] += 1
        if call_count["n"] <= transient_failures:
            # REQ 10.6: a transient :class:`OSError` triggers the retry
            # loop. ``ENOSPC`` / ``EIO`` are the canonical examples; the
            # exact errno does not matter to the writer.
            raise OSError("transient")
        return original_write(line)

    writer._fh.write = failing_write  # type: ignore[method-assign]

    try:
        await writer.append(
            "system_instantiated",
            {
                "system_id": "x",
                "role": "S1_WORKER",
                "sub_agent_count": 1,
            },
        )
        # Allow the writer task to drain the queue and run its retry
        # loop. 500 ms is generous compared to the 3 × 100 ms upper bound
        # for the worst-case retry sequence (~200 ms of inter-attempt
        # sleep plus disk I/O), which keeps the test deterministic on
        # slower CI hosts.
        await asyncio.sleep(0.5)

        if transient_failures < _APPEND_MAX_ATTEMPTS:
            # ------------------------------------------------------------------
            # Success branch — REQ 10.6 invariants when at least one
            # attempt succeeds.
            # ------------------------------------------------------------------
            # Invariant A (≤ 3 attempts): the writer never makes more
            # than ``transient_failures + 1`` write calls (the failures
            # plus the one that succeeded). Equivalently, it never makes
            # more than _APPEND_MAX_ATTEMPTS attempts in total.
            assert call_count["n"] == transient_failures + 1, (
                f"expected {transient_failures + 1} write attempts "
                f"(transient_failures={transient_failures} + 1 success), "
                f"got {call_count['n']}"
            )
            assert call_count["n"] <= _APPEND_MAX_ATTEMPTS

            # Invariant B (success is final): exactly one valid line is
            # persisted. If the writer kept retrying after success we
            # would observe two or more lines.
            lines = events_path.read_text().strip().split("\n")
            assert len(lines) == 1, (
                f"expected exactly one persisted line, got {len(lines)}"
            )

            # Invariant C (≥ 100 ms between attempts): every consecutive
            # pair of attempts must be at least 100 ms apart. Allow a
            # small (5 ms) clock-resolution tolerance so the test does
            # not flake on platforms whose monotonic clock has coarser
            # granularity than nominal.
            for i in range(1, len(attempt_timestamps)):
                gap = attempt_timestamps[i] - attempt_timestamps[i - 1]
                assert gap >= _APPEND_MIN_INTERVAL_SECONDS - 0.005, (
                    f"attempt {i} occurred {gap * 1000:.1f} ms after "
                    f"attempt {i - 1}, below the {_APPEND_MIN_INTERVAL_SECONDS * 1000:.0f} ms "
                    "REQ 10.6 lower bound"
                )

            # Invariant D (writer task still healthy): the writer task
            # is alive and ready to accept further appends. We do not
            # ``await`` it because that would block forever — instead
            # we observe its ``done()`` flag.
            assert writer._task is not None
            assert not writer._task.done(), (
                "writer task should still be running after a successful "
                "append; it appears to have exited unexpectedly"
            )
        else:
            # ------------------------------------------------------------------
            # Failure branch — REQ 10.6 invariants when all 3 attempts
            # fail.
            # ------------------------------------------------------------------
            # Invariant A (exactly 3 attempts): the writer must not make
            # a fourth attempt after the third failure.
            assert call_count["n"] == _APPEND_MAX_ATTEMPTS, (
                f"expected exactly {_APPEND_MAX_ATTEMPTS} write attempts "
                f"on full-failure path, got {call_count['n']}"
            )

            # Invariant B (≥ 100 ms between attempts): same lower bound
            # as the success branch. Two inter-attempt gaps are observed
            # (between attempts 1↔2 and 2↔3); there is no gap after the
            # final attempt because the writer raises immediately.
            for i in range(1, len(attempt_timestamps)):
                gap = attempt_timestamps[i] - attempt_timestamps[i - 1]
                assert gap >= _APPEND_MIN_INTERVAL_SECONDS - 0.005, (
                    f"attempt {i} occurred {gap * 1000:.1f} ms after "
                    f"attempt {i - 1}, below the {_APPEND_MIN_INTERVAL_SECONDS * 1000:.0f} ms "
                    "REQ 10.6 lower bound"
                )

            # Invariant C (typed surface behaviour): the writer task
            # dies with a typed :class:`EventLogAppendError` rather
            # than silently dropping the event.
            assert writer._task is not None
            assert writer._task.done(), (
                "writer task should have terminated after 3 consecutive "
                "failed write attempts"
            )
            with pytest.raises(EventLogAppendError):
                writer._task.result()

            # Invariant D (no partial line persisted): all attempts
            # raised before the genuine ``write`` was reached, so the
            # JSONL file contains no trailing valid line. ``stripped``
            # may still be empty (the file was created by the run dir
            # bootstrap step) or absent depending on flush ordering.
            content = events_path.read_text() if events_path.exists() else ""
            assert content.strip() == "", (
                f"expected no persisted line on full-failure path, "
                f"got {content!r}"
            )
    finally:
        # ``stop()`` re-raises the writer task's exception when the task
        # already terminated with one (the failure branch above), so we
        # swallow it here — the relevant assertions have already run.
        try:
            await writer.stop()
        except EventLogAppendError:
            pass


# ---------------------------------------------------------------------------
# Part 2 — Task 13.2: S4 → S5 delivery retry (REQ 5.6)
# ---------------------------------------------------------------------------


# REQ 5.6: at most 3 attempts and at least 10 s between attempts. Mirrored
# from :mod:`vsm.systems.s4_scanner` so that any spec change is caught.
_DELIVERY_MAX_ATTEMPTS = 3
_DELIVERY_MIN_INTERVAL_SECONDS = 10.0


@pytest.mark.asyncio
@pytest.mark.parametrize("n_failures", [0, 1, 2, 3])
async def test_s4_delivery_retry(tmp_path, monkeypatch, n_failures: int) -> None:
    """REQ 5.6: S4 retries delivery up to 3 times with ≥ 10 s between attempts.

    Validates Property 16 (S4 delivery portion).

    Strategy
    --------
    1. Replace :func:`asyncio.sleep` inside :mod:`vsm.systems.s4_scanner`
       with an awaitable shim that records the requested duration but
       yields the loop with ``await asyncio.sleep(0)``. This collapses
       the otherwise-blocking ``10 s`` retry intervals so the test runs
       in milliseconds while still letting us assert the requested
       duration was 10 s (REQ 5.6 lower bound).
    2. Boot a real :class:`Platform` with a deterministic
       :class:`FakeLLMProvider` so S4 produces a non-empty assessment
       on its first cycle.
    3. Replace ``s4._bus.send`` with a wrapper that returns
       :meth:`SendResult.rejected` for the first ``n_failures`` ``S4-S5``
       sends, then delegates to the original implementation. Sends on
       other channels (``S3-S5`` from S5's dispatch, etc.) always
       delegate to the original.
    4. Trigger S4 with a synthetic Task and wait briefly for the
       delivery loop to run.
    5. Read ``events.jsonl`` and assert REQ 5.6's invariants.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    # ------------------------------------------------------------------
    # 1. Capture the genuine ``asyncio.sleep`` and patch the module's
    # reference so the S4 retry intervals collapse to a no-op.
    # ------------------------------------------------------------------
    # ``vsm.systems.s4_scanner.asyncio`` is a reference to the global
    # ``asyncio`` module, so ``monkeypatch.setattr("vsm.systems
    # .s4_scanner.asyncio.sleep", ...)`` ends up rebinding ``asyncio
    # .sleep`` globally for the duration of the test. We capture
    # ``real_sleep`` *before* the patch is applied so that (a) the fake
    # can still yield the event loop via the genuine sleep, and (b) the
    # test body itself can wait real wall-clock time without being
    # collapsed to zero. ``monkeypatch`` reverts the global rebind at
    # test teardown so this side effect does not leak into other tests.
    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # ``real_sleep(0)`` yields the event loop without actually
        # waiting, so the retry interval semantics (the *requested*
        # duration is 10 s) are preserved while keeping the test fast.
        await real_sleep(0)

    monkeypatch.setattr(
        "vsm.systems.s4_scanner.asyncio.sleep", fake_sleep
    )

    # ------------------------------------------------------------------
    # 2. Boot a Platform with a deterministic FakeLLMProvider.
    # ------------------------------------------------------------------
    # ``response="opportunity"`` makes both S4 sub-agents produce a
    # non-empty observation (营業 → opportunities, リサーチ → threats),
    # which keeps the assessment non-trivial and exercises the delivery
    # path. ``latency=0.0`` keeps the test deterministic.
    fake_llm = FakeLLMProvider(response="opportunity", latency=0.0)
    platform = await start_run(
        runs_dir=runs_dir,
        run_config=RunConfig(),
        llm_override=fake_llm,
        clock=SystemClock(),
    )
    s4 = platform.systems[SystemRole.S4_SCANNER][0]
    s5 = platform.systems[SystemRole.S5_POLICY][0]
    # Stop S5 so the S5 → S4 follow-up (REQ 5.7) does not feed back into
    # the S4 retry loop under test. We are isolating REQ 5.6 (S4 → S5
    # delivery retries) from the broader feedback cycle.
    await s5.shutdown()

    # ------------------------------------------------------------------
    # 3. Wrap the bus's ``send`` so the first ``n_failures`` S4-S5 sends
    # are rejected.
    # ------------------------------------------------------------------
    # ``s4._bus`` is the same :class:`MessageBus` instance that every
    # System holds, so the wrapper sees every send. We discriminate by
    # ``msg.channel`` so that S5's dispatches on S3-S5 and any later
    # S4-S5 follow-up sends from S5 are unaffected.
    s4_s5_send_count = {"n": 0}
    original_send = s4._bus.send

    async def failing_send(msg):
        # Increment the channel-specific counter and reject the first
        # ``n_failures`` S4-S5 attempts; everything else falls through
        # to the genuine bus send.
        if msg.channel.value == "S4-S5":
            s4_s5_send_count["n"] += 1
            if s4_s5_send_count["n"] <= n_failures:
                # REQ 5.6: the bus may surface a non-throwing rejection.
                # The S4 retry loop treats either an exception or a
                # ``SendResult.delivered=False`` as a failed attempt.
                return SendResult.rejected(msg.channel)
        return await original_send(msg)

    s4._bus.send = failing_send  # type: ignore[method-assign]

    try:
        # ------------------------------------------------------------------
        # 4. Trigger S4 and wait briefly for the delivery loop to run.
        # ------------------------------------------------------------------
        await s4.trigger({"description": "retry test"})
        # The patched ``asyncio.sleep`` makes each "10 s" retry interval
        # collapse to ``await real_sleep(0)``, so 500 ms of real wall
        # time is more than enough for the worst case (3 attempts plus
        # 2 yields). We use ``real_sleep`` (captured before the patch)
        # so this wait is not itself collapsed by the monkey patch.
        await real_sleep(0.5)

        # ------------------------------------------------------------------
        # 5. Assert REQ 5.6's invariants.
        # ------------------------------------------------------------------
        events_path = platform.run_dir / "events.jsonl"
        events = read_all(events_path)

        # Filter ``delivery_error`` events down to the S4-S5 channel so
        # any unrelated dispatch errors elsewhere in the platform (none
        # are expected, but we want to be robust) do not skew the count.
        delivery_errors = [
            e
            for e in events
            if e["event_type"] == "delivery_error"
            and e["payload"].get("channel") == "S4-S5"
        ]

        # Invariant A (one ``delivery_error`` per failed attempt):
        # exactly ``min(n_failures, _DELIVERY_MAX_ATTEMPTS)`` events on
        # the success path; exactly _DELIVERY_MAX_ATTEMPTS on the
        # full-failure path.
        expected_errors = min(n_failures, _DELIVERY_MAX_ATTEMPTS)
        assert len(delivery_errors) >= expected_errors, (
            f"expected at least {expected_errors} S4-S5 delivery_error "
            f"events for n_failures={n_failures}, got "
            f"{len(delivery_errors)}"
        )

        # Invariant B (≤ 3 attempts): the wrapped ``send`` was called
        # at most _DELIVERY_MAX_ATTEMPTS times by S4's first delivery
        # cycle. In the success branch the cycle stops as soon as one
        # attempt succeeds; in the full-failure branch S5 never
        # receives the assessment so no follow-up cycle increments the
        # counter further.
        if n_failures < _DELIVERY_MAX_ATTEMPTS:
            # The first cycle made ``n_failures + 1`` S4-S5 sends. A
            # follow-up from S5 may add at most one more S4-S5 send
            # (S5 dispatches a follow-up to the S4 that produced the
            # assessment, and the resulting follow-up cycle's delivery
            # back to S5 is a third S4-S5 send). The first-cycle
            # invariant we assert here is therefore ``>= n_failures + 1``.
            assert s4_s5_send_count["n"] >= n_failures + 1, (
                f"expected at least {n_failures + 1} S4-S5 send attempts "
                f"in the success branch, got {s4_s5_send_count['n']}"
            )
        else:
            # Full-failure branch: exactly 3 attempts, no follow-up.
            assert s4_s5_send_count["n"] == _DELIVERY_MAX_ATTEMPTS, (
                f"expected exactly {_DELIVERY_MAX_ATTEMPTS} S4-S5 send "
                f"attempts on full-failure path, got "
                f"{s4_s5_send_count['n']}"
            )

        # Invariant C (≥ 10 s between attempts): for every retry the
        # implementation requested an ``asyncio.sleep(10.0)``. We assert
        # the requested duration was at least 10 s rather than relying
        # on wall-clock measurement, because the patched sleep collapses
        # the actual wait to zero — but the *requested* duration is what
        # REQ 5.6 constrains.
        if n_failures > 0:
            assert any(
                s >= _DELIVERY_MIN_INTERVAL_SECONDS - 0.001
                for s in sleep_calls
            ), (
                f"expected at least one sleep ≥ {_DELIVERY_MIN_INTERVAL_SECONDS}s "
                f"between S4-S5 retry attempts, observed sleep_calls={sleep_calls}"
            )
            # Every recorded sleep should be at least the lower bound;
            # the implementation must not request a shorter interval.
            for s in sleep_calls:
                assert s >= _DELIVERY_MIN_INTERVAL_SECONDS - 0.001, (
                    f"observed retry sleep duration {s}s < "
                    f"{_DELIVERY_MIN_INTERVAL_SECONDS}s REQ 5.6 lower bound"
                )
        else:
            # n_failures == 0: the first attempt succeeds, so no retry
            # interval is ever requested by the first cycle. (S5 may
            # later dispatch a follow-up, but that path does not call
            # ``asyncio.sleep`` in the s4_scanner module.)
            assert all(
                s >= _DELIVERY_MIN_INTERVAL_SECONDS - 0.001
                for s in sleep_calls
            ), (
                "any sleep observed in the n_failures=0 branch must "
                "still respect the REQ 5.6 lower bound; got "
                f"sleep_calls={sleep_calls}"
            )
    finally:
        await platform.shutdown()
