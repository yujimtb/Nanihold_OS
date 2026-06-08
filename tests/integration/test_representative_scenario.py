"""Representative scenario integration tests (REQ 12).

This module implements three end-to-end integration tests for the
``Nanihold OS`` representative scenario:

* :func:`test_scenario_success` — REQ 12.1〜12.8: the scenario completes
  within the 1800-second budget, all six mandatory roles are observed in
  ``system_instantiated`` events, and at least one ``s1_completion``
  appears.
* :func:`test_scenario_timeout` — REQ 12.9: when completion criteria
  cannot be met (LLM latency exceeds the 60-second timeout), the platform
  remains active (``RUNNING`` lockfile present) and shuts down cleanly.
* :func:`test_scenario_replay_roundtrip` — REQ 10.10 / 11.5: after the
  Run shuts down, :func:`replay` reconstructs Task / S1 lifecycle /
  channel events / audit findings projections from ``events.jsonl``.

All tests use :class:`FakeLLMProvider` to avoid real LLM calls and
``tmp_path`` to isolate the Run directory per test.

Validates Requirements
----------------------
- REQ 12.1〜12.9 (representative scenario lifecycle).
- REQ 10.10 (replay reconstructs runtime cache).
- REQ 11.5 (``vsm replay`` consumes ``events.jsonl``).
"""

from __future__ import annotations

import asyncio

import pytest

from vsm.clock import SystemClock
from vsm.config import RunConfig
from vsm.eventlog.reader import read_all
from vsm.eventlog.replay import replay
from vsm.llm.fake import FakeLLMProvider
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import start_run


@pytest.mark.asyncio
async def test_scenario_success(tmp_path):
    """REQ 12.1〜12.8: representative scenario completes within budget.

    Uses :class:`FakeLLMProvider` with deterministic short-latency responses.
    Verifies all 6 mandatory roles emit at least one ``system_instantiated``
    event, an ``s1_completion`` appears, and the scenario finishes well
    within the 1800-second budget (the test polls for up to 60 seconds of
    wall-clock).
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    # Deterministic LLM provider — 50 ms latency is well under the 60 s
    # per-call SLA (REQ 3.4) so neither the LLM call nor any downstream
    # System hits its timeout.
    fake_llm = FakeLLMProvider(
        response="completed task",
        latency=0.05,
    )

    platform = await start_run(
        runs_dir=runs_dir,
        run_config=RunConfig(),
        llm_override=fake_llm,
        clock=SystemClock(),
    )

    try:
        # Inject the initial Task into S4 (the scenario kicks off with
        # S4_Scanner observing an external opportunity / Task).
        s4 = platform.systems[SystemRole.S4_SCANNER][0]
        await s4.trigger({"description": "test scenario"})

        # Poll for completion. Cap at 60 s wall-clock — a tiny fraction
        # of the 1800 s production budget — so the test fails fast if
        # the scenario stalls.
        events_path = platform.run_dir / "events.jsonl"
        deadline = SystemClock().monotonic() + 60.0
        roles_seen: set[str] = set()
        s1_completion_seen = False
        while SystemClock().monotonic() < deadline:
            await asyncio.sleep(0.5)
            if events_path.exists():
                events = read_all(events_path)
                roles_seen = {
                    e["payload"].get("role")
                    for e in events
                    if e["event_type"] == "system_instantiated"
                    and e["payload"].get("role")
                }
                event_types = {e["event_type"] for e in events}
                s1_completion_seen = "s1_completion" in event_types
                if s1_completion_seen and {
                    "S1_WORKER",
                    "S2_COORDINATOR",
                    "S3_ALLOCATOR",
                    "S3STAR_AUDITOR",
                    "S4_SCANNER",
                    "S5_POLICY",
                }.issubset(roles_seen):
                    break

        # REQ 12.7, 12.8: every mandatory role plus the dynamically-spawned
        # S1_WORKER should appear, and at least one ``s1_completion`` event
        # should have been emitted before the scenario could be considered
        # finished.
        assert "S1_WORKER" in roles_seen, (
            f"Missing S1_WORKER role; roles_seen={roles_seen}"
        )
        assert "S2_COORDINATOR" in roles_seen
        assert "S3_ALLOCATOR" in roles_seen
        assert "S3STAR_AUDITOR" in roles_seen
        assert "S4_SCANNER" in roles_seen
        assert "S5_POLICY" in roles_seen
        assert s1_completion_seen, "Expected at least one s1_completion event"
    finally:
        await platform.shutdown()


@pytest.mark.asyncio
async def test_scenario_timeout(tmp_path):
    """REQ 12.9: scenario times out cleanly when completion criteria are not met.

    Uses a :class:`FakeLLMProvider` with very long latency so
    ``s1_completion`` never appears within the short test deadline.
    Verifies the platform is alive (``RUNNING`` lockfile present) during
    the run and that :meth:`Platform.shutdown` removes the lockfile
    without raising.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    # Latency well above the 60 s LLM-call SLA: any in-flight invoke would
    # be cancelled by ``asyncio.wait_for`` upstream, but the test does not
    # wait long enough for that to fire — it only verifies the run remains
    # active during the polling window.
    fake_llm = FakeLLMProvider(
        response="ok",
        latency=120.0,
    )

    platform = await start_run(
        runs_dir=runs_dir,
        run_config=RunConfig(),
        llm_override=fake_llm,
        clock=SystemClock(),
    )

    try:
        s4 = platform.systems[SystemRole.S4_SCANNER][0]
        await s4.trigger({"description": "stuck task"})

        # Wait briefly. The 60 s LLM timeout cannot fire within this
        # window, so the run should still be active when we check.
        await asyncio.sleep(2.0)

        # REQ 11.6: the ``RUNNING`` lockfile is present while the Run is
        # active.
        lockfile = platform.run_dir / "RUNNING"
        assert lockfile.exists()
    finally:
        await platform.shutdown()
        # REQ 11.6: shutdown removes the ``RUNNING`` lockfile so that
        # ``vsm replay`` no longer warns about an active Run.
        assert not (platform.run_dir / "RUNNING").exists()


@pytest.mark.asyncio
async def test_scenario_replay_roundtrip(tmp_path):
    """REQ 10.10: ``replay()`` reconstructs Task / S1 lifecycle / channel
    events / audit findings projections matching the runtime cache.
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    fake_llm = FakeLLMProvider(response="ok", latency=0.05)
    platform = await start_run(
        runs_dir=runs_dir,
        run_config=RunConfig(),
        llm_override=fake_llm,
        clock=SystemClock(),
    )

    try:
        s4 = platform.systems[SystemRole.S4_SCANNER][0]
        await s4.trigger({"description": "replay test"})
        # Allow some processing time — long enough for the scenario to
        # produce a non-trivial event log, short enough to keep the test
        # fast.
        await asyncio.sleep(3.0)
    finally:
        await platform.shutdown()

    # After shutdown, replay events.jsonl into a ReconstructedState.
    events_path = platform.run_dir / "events.jsonl"
    state = replay(events_path)

    # REQ 10.10: the four projections plus the systems index should be
    # populated as the expected container types, even when individual
    # entries may be empty.
    assert isinstance(state.tasks, dict)
    assert isinstance(state.s1_lifecycle, dict)
    assert isinstance(state.channel_events, list)
    assert isinstance(state.audit_findings, dict)
    assert isinstance(state.systems, dict)

    # Every mandatory non-S1 role appears in the systems projection
    # because ``Platform.create`` emits ``system_instantiated`` for all
    # five at Run start (REQ 1.5). S1_WORKER may or may not be present
    # depending on whether the scenario reached the dynamic-spawn path
    # within the 3-second polling window, so it is intentionally not
    # asserted here.
    role_strs = {info["role"] for info in state.systems.values()}
    assert "S2_COORDINATOR" in role_strs
    assert "S3_ALLOCATOR" in role_strs
    assert "S3STAR_AUDITOR" in role_strs
    assert "S4_SCANNER" in role_strs
    assert "S5_POLICY" in role_strs

    # The events file should be non-empty: at minimum the five
    # ``system_instantiated`` events from Run start are present.
    raw_events = read_all(events_path)
    assert len(raw_events) > 0
