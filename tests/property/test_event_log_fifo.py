"""Property 6 (FIFO append order). Validates Requirements: 10.8.

Feature: Nanihold OS, Property 6
Validates: Requirements 10.8

This test verifies the single-writer FIFO discipline of
:class:`vsm.eventlog.writer.EventLogWriter`. Per REQ 10.8, given any
sequence of N enqueue operations issued by the same producer task,
the resulting ``events.jsonl`` file must satisfy two invariants:

1. The ``seq`` field of every recorded line is monotonically assigned
   starting from 0, i.e. lines carry ``seq = 0, 1, ..., N-1`` in that
   exact order.
2. The line order in the file matches the enqueue order so that the
   first event appended also appears on the first line, the second on
   the second line, and so on.

Property 6 is the safety property of the writer: if it ever held
``seq_i == seq_j`` for ``i != j`` or wrote events out of enqueue order
the Event_Log would no longer be a faithful audit trail and replay
(REQ 10.10) would diverge from runtime cached state.

Notes on the testing harness
----------------------------
* This test uses ``pytest.mark.parametrize`` over a representative range
  of ``N`` values rather than Hypothesis ``@given``. ``@given`` and
  ``pytest-asyncio`` interact awkwardly (the event loop fixture is per
  test, not per Hypothesis example) so the parametrize approach gives a
  more robust harness while still exercising Property 6 across a span
  of N values from the trivial (``N == 1``) up to a moderately large
  batch (``N == 50``).
* ``asyncio_mode = "auto"`` is set in ``pyproject.toml``; the explicit
  ``@pytest.mark.asyncio`` is kept here for clarity and to remain
  robust if the project mode changes in the future.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from vsm.clock import SystemClock
from vsm.eventlog.writer import EventLogWriter


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [1, 2, 5, 10, 25, 50])
async def test_fifo_order_and_seq_monotonic(tmp_path, n: int) -> None:
    """REQ 10.8: ``seq`` is monotonic and file order == enqueue order.

    Strategy
    --------
    For each parametrized ``n`` in ``{1, 2, 5, 10, 25, 50}``:

    1. Construct an :class:`EventLogWriter` bound to ``tmp_path/events.jsonl``
       and start its writer task.
    2. Sequentially ``await writer.append("system_instantiated", payload_i)``
       for ``i`` in ``[0, n)``, embedding ``i`` in the payload's
       ``system_id`` so the file-order assertion can be made
       payload-aware (not only ``seq``-aware).
    3. Stop the writer (which cancels the writer task and flushes /
       closes the file) so that all enqueued events have been drained
       to disk.
    4. Read ``events.jsonl`` line by line and assert that:

       * exactly ``n`` lines were written;
       * line ``i`` carries ``seq == i`` (REQ 10.8 monotonicity); and
       * line ``i`` carries the payload that was enqueued ``i``-th
         (REQ 10.8 enqueue-order preservation).
    """
    path = tmp_path / "events.jsonl"

    writer = EventLogWriter(run_id="run-fifo", path=path, clock=SystemClock())
    await writer.start()
    try:
        # Sequentially enqueue N events from a single producer task. The
        # producer-task identity is what makes the enqueue order well
        # defined: even though ``append`` is async, the awaits run in
        # program order, so the queue receives ``(0, 1, ..., n-1)``.
        for i in range(n):
            await writer.append(
                "system_instantiated",
                {
                    "system_id": f"sys-{i:04d}",
                    "role": "S1_WORKER",
                    "sub_agent_count": 1,
                },
            )
        # Yield control so the writer task can drain the queue before
        # ``stop`` cancels it. Without this drain step, ``stop`` would
        # cancel the writer task with events still pending in the queue
        # and they would never be flushed to disk. The exact sleep is
        # generous (500 ms) to tolerate slower CI runners; the test is
        # bounded by ``n <= 50`` so this does not blow up runtime
        # materially.
        await asyncio.sleep(0.5)
    finally:
        # ``stop`` drains/cancels the writer task and closes the file
        # handle, guaranteeing every successful ``append`` has been
        # flushed + fsynced to disk before we read it back.
        await writer.stop()

    # File-level invariants -------------------------------------------------
    raw = path.read_text(encoding="utf-8").strip()
    lines = raw.split("\n") if raw else []
    assert len(lines) == n, (
        f"expected {n} lines in events.jsonl, got {len(lines)}"
    )

    seqs: list[int] = []
    system_ids: list[str] = []
    for line in lines:
        evt = json.loads(line)
        seqs.append(evt["seq"])
        system_ids.append(evt["payload"]["system_id"])

    # REQ 10.8 invariant 1: ``seq`` is the line index, monotonically
    # increasing from 0 to n-1 with no gaps and no duplicates.
    assert seqs == list(range(n)), (
        f"seq monotonicity violated: expected {list(range(n))}, got {seqs}"
    )
    # REQ 10.8 invariant 2: the payload at line ``idx`` is the
    # ``idx``-th payload that was enqueued (FIFO order preservation).
    assert system_ids == [f"sys-{i:04d}" for i in range(n)], (
        f"FIFO order violated: expected sys-0000..sys-{n - 1:04d} in "
        f"order, got {system_ids}"
    )
