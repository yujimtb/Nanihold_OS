"""Property 5 (Event_Log round-trip). Validates Requirements: 10.1, 10.9, 10.10.

This module exercises :func:`vsm.eventlog.replay.replay` against event
sequences appended through the production :class:`EventLogWriter`. The
property under test is the round-trip equality between the runtime cache
projections that the live Systems would have produced and the
:class:`vsm.runtime.state.ReconstructedState` that ``replay`` produces from
``events.jsonl`` alone (REQ 10.10).

Strategy
--------
Rather than a single Hypothesis-driven generator, the scenarios are split
into hand-written cases so that each REQ 10.10 projection is exercised in
isolation and a regression in any one of them produces a sharply-localised
failure. The six scenarios cover, in order:

1. Empty Run → all four projections are empty (REQ 10.1 baseline).
2. ``task_submitted`` → ``state.tasks`` holds one entry in ``submitted``.
3. ``task_submitted`` + ``task_state_changed`` → terminal state advances.
4. ``s1_instantiated`` → ``s1_assignment_sent`` → ``s1_completion`` →
   ``state.s1_lifecycle[s1_id]`` carries the three lifecycle events in
   append order (REQ 7.4 / 7.7 / 7.8 / 10.10).
5. ``channel_message`` + ``channel_rejected`` → ``state.channel_events``
   preserves append order across both outcomes (REQ 2.7 / 2.9 / 10.10).
6. Three ``audit_finding`` events with two distinct ``finding_id`` values
   → ``state.audit_findings`` contains two entries and the *first*
   observation per ``finding_id`` is authoritative (REQ 9.4 / 10.10).

A seventh scenario (``test_replay_systems_index``) verifies that
``system_instantiated`` for an ``S1_WORKER`` role also seeds the
``s1_lifecycle`` projection, matching the replay handler in
``vsm/eventlog/replay.py``.

Notes
-----
* The helper :func:`_write_events` exercises the *real*
  :class:`EventLogWriter`, which means every event in these scenarios
  passes through ``validate_event_payload`` (REQ 10.7) before being
  serialised. This keeps the replay round-trip honest: any payload shape
  that replay assumes must also be a shape the writer is willing to emit.
* ``await asyncio.sleep(0.2)`` before ``writer.stop()`` is required because
  ``stop`` cancels the writer task rather than draining it; the 200 ms
  gap is comfortably above the 100 ms append-visibility SLA (REQ 10.5)
  even on slow CI runners.
* ``asyncio_mode = "auto"`` is configured in ``pyproject.toml``; the
  explicit ``@pytest.mark.asyncio`` decoration is kept here for clarity
  and to remain robust if the project mode ever changes.
"""

from __future__ import annotations

import asyncio

import pytest

from vsm.clock import SystemClock
from vsm.eventlog.replay import replay
from vsm.eventlog.writer import EventLogWriter


async def _write_events(path, events_list):
    """Append ``events_list`` to ``path`` via :class:`EventLogWriter`.

    Each element of ``events_list`` is a ``(event_type, payload)`` tuple.
    The helper boots a writer bound to ``run-replay`` (a fixed run_id that
    satisfies REQ 10.2's 1..64 ASCII range), enqueues every event in
    program order, sleeps 200 ms to let the single-writer task drain the
    queue (REQ 10.5 / 10.8), and finally stops the writer. The assertions
    in each test then run against the on-disk JSONL file alone, so this
    helper is the only place where the writer touches replay's input.
    """
    writer = EventLogWriter(run_id="run-replay", path=path, clock=SystemClock())
    await writer.start()
    try:
        for et, payload in events_list:
            await writer.append(et, payload)
        # REQ 10.5: 100 ms SLA per append; 200 ms is enough headroom for
        # the writer task to drain the queue before stop() cancels it.
        await asyncio.sleep(0.2)
    finally:
        await writer.stop()


@pytest.mark.asyncio
async def test_replay_empty(tmp_path):
    """Empty Run → all four REQ 10.10 projections are empty.

    Validates: Requirements 10.1, 10.10.
    """
    path = tmp_path / "events.jsonl"
    # REQ 10.1: replay reads exclusively from events.jsonl. An empty file
    # is a valid baseline (a Run that came up but has not yet emitted any
    # events) and must round-trip to the empty ``ReconstructedState``.
    path.touch()
    state = replay(path)
    assert state.tasks == {}
    assert state.s1_lifecycle == {}
    assert state.channel_events == []
    assert state.audit_findings == {}


@pytest.mark.asyncio
async def test_replay_single_task(tmp_path):
    """Single ``task_submitted`` → ``state.tasks`` has one entry.

    Validates: Requirements 4.6, 10.10.
    """
    path = tmp_path / "events.jsonl"
    await _write_events(
        path,
        [
            (
                "task_submitted",
                {
                    "task_id": "t1",
                    "run_id": "run-x",
                    "description": "hello",
                    "file_paths": [],
                    "submitted_at": "2025-01-01T00:00:00.000Z",
                },
            ),
        ],
    )
    state = replay(path)
    assert "t1" in state.tasks
    # REQ 10.10: the ``tasks`` projection records the task's *current*
    # state. With only a ``task_submitted`` event, the state is the
    # initial ``submitted`` value of :class:`TaskState`.
    assert state.tasks["t1"]["state"] == "submitted"


@pytest.mark.asyncio
async def test_replay_task_state_transition(tmp_path):
    """``task_submitted`` + ``task_state_changed`` → terminal state wins.

    Validates: Requirements 10.5, 10.10.
    """
    path = tmp_path / "events.jsonl"
    await _write_events(
        path,
        [
            (
                "task_submitted",
                {
                    "task_id": "t1",
                    "run_id": "run-x",
                    "description": "hello",
                    "file_paths": [],
                    "submitted_at": "2025-01-01T00:00:00.000Z",
                },
            ),
            (
                "task_state_changed",
                {
                    "task_id": "t1",
                    "from_state": "submitted",
                    "to_state": "completed",
                },
            ),
        ],
    )
    state = replay(path)
    # REQ 10.10: replay must apply state transitions in seq order so that
    # the ``tasks`` projection converges to the *latest* observed state,
    # not the first one.
    assert state.tasks["t1"]["state"] == "completed"


@pytest.mark.asyncio
async def test_replay_s1_lifecycle(tmp_path):
    """S1 lifecycle: instantiated → assignment_sent → completion.

    Validates: Requirements 7.4, 7.7, 7.8, 10.10.
    """
    path = tmp_path / "events.jsonl"
    await _write_events(
        path,
        [
            (
                "s1_instantiated",
                {
                    "s1_id": "s1-a",
                    "specialization": "frontend",
                    "initial_assignment": "task1",
                },
            ),
            (
                "s1_assignment_sent",
                {
                    "s1_id": "s1-a",
                    "work_item_id": "wi-1",
                    "assignment": {"x": 1},
                },
            ),
            (
                "s1_completion",
                {
                    "s1_id": "s1-a",
                    "work_item_id": "wi-1",
                    "result": {"success": True},
                },
            ),
        ],
    )
    state = replay(path)
    # REQ 10.10: the ``s1_lifecycle`` projection is keyed by ``s1_id`` and
    # carries the ordered history of lifecycle events (REQ 1.6, 7.4, 7.7,
    # 7.8). The normalised ``event_type`` strings on
    # :class:`S1LifecycleEvent` strip the ``s1_`` prefix so callers can
    # compare projections without coupling to the wire-level event_type.
    assert "s1-a" in state.s1_lifecycle
    events = state.s1_lifecycle["s1-a"]
    assert [e.event_type for e in events] == [
        "instantiated",
        "assignment_sent",
        "completion",
    ]


@pytest.mark.asyncio
async def test_replay_channel_events_order(tmp_path):
    """``channel_message`` + ``channel_rejected`` interleave in append order.

    Validates: Requirements 2.7, 2.9, 10.10.
    """
    path = tmp_path / "events.jsonl"
    await _write_events(
        path,
        [
            (
                "channel_message",
                {
                    "sender": "s4",
                    "receiver": "s5",
                    "channel": "S4-S5",
                    "payload": {"k": 1},
                },
            ),
            (
                "channel_rejected",
                {
                    "sender": "x",
                    "receiver": "y",
                    "channel": "S1-S2",
                },
            ),
        ],
    )
    state = replay(path)
    assert len(state.channel_events) == 2
    # REQ 10.10: the ``channel_events`` projection preserves append order
    # across both successful deliveries and rejections.
    assert state.channel_events[0].event_type == "channel_message"
    assert state.channel_events[1].event_type == "channel_rejected"
    # REQ 2.7 / 2.8: the rejected message body is intentionally not
    # persisted; the rejection record carries channel routing only, so
    # replay surfaces ``payload=None`` for ``channel_rejected``.
    assert state.channel_events[1].payload is None


@pytest.mark.asyncio
async def test_replay_audit_finding_dedup(tmp_path):
    """Three ``audit_finding`` events with two ids → first-write-wins per id.

    Validates: Requirements 9.4, 10.10.
    """
    path = tmp_path / "events.jsonl"
    await _write_events(
        path,
        [
            (
                "audit_finding",
                {"finding_id": "f1", "s1_id": "s1-x", "content": "first"},
            ),
            (
                "audit_finding",
                {"finding_id": "f1", "s1_id": "s1-x", "content": "second"},
            ),
            (
                "audit_finding",
                {"finding_id": "f2", "s1_id": "s1-y", "content": "third"},
            ),
        ],
    )
    state = replay(path)
    # REQ 10.10: ``audit_findings`` is indexed by ``finding_id``; two
    # distinct ids produce two entries.
    assert len(state.audit_findings) == 2
    # REQ 9.4 / 10.10: replay treats the *first* observation per
    # ``finding_id`` as authoritative so retried emissions cannot
    # overwrite the original seq attribution.
    assert state.audit_findings["f1"].content == "first"
    assert state.audit_findings["f2"].content == "third"


@pytest.mark.asyncio
async def test_replay_systems_index(tmp_path):
    """``system_instantiated`` populates ``state.systems`` and seeds S1 lifecycle.

    Validates: Requirements 1.5, 1.6, 11.1, 10.10.
    """
    path = tmp_path / "events.jsonl"
    await _write_events(
        path,
        [
            (
                "system_instantiated",
                {
                    "system_id": "sys-1",
                    "role": "S5_POLICY",
                    "sub_agent_count": 1,
                },
            ),
            (
                "system_instantiated",
                {
                    "system_id": "sys-2",
                    "role": "S1_WORKER",
                    "sub_agent_count": 1,
                },
            ),
        ],
    )
    state = replay(path)
    # ``systems`` is the convenience projection used by ``vsm status``
    # (REQ 11.1); both Systems must be present after replay.
    assert "sys-1" in state.systems
    assert "sys-2" in state.systems
    assert state.systems["sys-1"]["role"] == "S5_POLICY"
    # REQ 10.10: any S1_WORKER instantiation — whether it arrives as
    # ``s1_instantiated`` or as ``system_instantiated`` with
    # ``role == "S1_WORKER"`` — must seed the ``s1_lifecycle`` projection
    # so Property 5's lifecycle history is well-defined for both forms.
    assert "sys-2" in state.s1_lifecycle
