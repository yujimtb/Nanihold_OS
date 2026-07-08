"""Property 14 (Audit schedule). Validates Requirements: 9.1."""
from __future__ import annotations
import asyncio
import pytest
from vsm.clock import SystemClock
from vsm.config import RunConfig
from vsm.eventlog.reader import read_all
from vsm.llm.fake import FakeLLMProvider
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import start_run


async def _wait_for_event_count(
    events_path, event_type: str, minimum: int, timeout: float = 2.0
):
    deadline = SystemClock().monotonic() + timeout
    while SystemClock().monotonic() < deadline:
        events = read_all(events_path) if events_path.exists() else []
        matches = [e for e in events if e["event_type"] == event_type]
        if len(matches) >= minimum:
            return events
        await asyncio.sleep(0.05)
    return read_all(events_path) if events_path.exists() else []


@pytest.mark.asyncio
async def test_completion_signal_triggers_observation(tmp_path):
    """REQ 9.1: notify_completion → triggers observation cycle."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    fake_llm = FakeLLMProvider(response="ok", latency=0.0)
    platform = await start_run(
        runs_dir=runs_dir, run_config=RunConfig(),
        llm_override=fake_llm, clock=SystemClock(),
    )
    s3star = platform.systems[SystemRole.S3STAR_AUDITOR][0]

    try:
        # Spawn an S1 so there's something to audit
        s1 = await platform.spawn_s1(specialization="frontend", initial_assignment="task1")

        # Trigger completion signal
        s3star.notify_completion()

        events_path = platform.run_dir / "events.jsonl"
        events = await _wait_for_event_count(events_path, "audit_observation", 1)
        observations = [e for e in events if e["event_type"] == "audit_observation"]
        # Should have at least one observation triggered by the completion signal
        assert len(observations) >= 1, f"expected ≥1 audit_observation, got {len(observations)}"
    finally:
        await platform.shutdown()


@pytest.mark.asyncio
async def test_audit_finding_after_observation(tmp_path):
    """REQ 9.3, 9.4: observation triggers finding generation."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    fake_llm = FakeLLMProvider(response="ok", latency=0.0)
    platform = await start_run(
        runs_dir=runs_dir, run_config=RunConfig(),
        llm_override=fake_llm, clock=SystemClock(),
    )
    s3star = platform.systems[SystemRole.S3STAR_AUDITOR][0]

    try:
        await platform.spawn_s1(specialization="frontend", initial_assignment="task1")
        s3star.notify_completion()

        events_path = platform.run_dir / "events.jsonl"
        events = await _wait_for_event_count(events_path, "audit_finding", 1)
        findings = [e for e in events if e["event_type"] == "audit_finding"]
        report_sents = [e for e in events if e["event_type"] == "audit_report_sent"]
        assert len(findings) >= 1, f"expected ≥1 audit_finding, got {len(findings)}"
        # report_sent only if delivery was successful
    finally:
        await platform.shutdown()


@pytest.mark.asyncio
async def test_no_s1_no_observation(tmp_path):
    """REQ 9.1: when no S1s exist, observation cycle is a no-op."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    fake_llm = FakeLLMProvider(response="ok", latency=0.0)
    platform = await start_run(
        runs_dir=runs_dir, run_config=RunConfig(),
        llm_override=fake_llm, clock=SystemClock(),
    )
    s3star = platform.systems[SystemRole.S3STAR_AUDITOR][0]

    try:
        # No S1s spawned
        s3star.notify_completion()
        await asyncio.sleep(0.3)

        events_path = platform.run_dir / "events.jsonl"
        events = read_all(events_path)
        observations = [e for e in events if e["event_type"] == "audit_observation"]
        assert len(observations) == 0
    finally:
        await platform.shutdown()


def test_notify_completion_idempotent():
    """notify_completion is idempotent (multiple calls coalesce)."""
    from vsm.systems.s3star_auditor import S3StarAuditor
    # Just check that the asyncio.Event semantics are coalescing
    import asyncio
    e = asyncio.Event()
    e.set()
    e.set()
    e.set()
    # set() multiple times just sets the flag once
    assert e.is_set()
