"""Property 10 (Mandatory systems verification). Validates Requirements: 1.7, 13.1, 13.2, 13.3."""

from __future__ import annotations

import itertools

import pytest

from vsm.clock import SystemClock
from vsm.config import RunConfig
from vsm.errors import ConfigError
from vsm.llm.fake import FakeLLMProvider
from vsm.roles import MANDATORY_ROLES, SystemRole
from vsm.runtime.lifecycle import start_run


def _build_config_with_missing(missing_roles: list[SystemRole]) -> RunConfig:
    """Build a RunConfig with given mandatory roles set to 0."""
    counts = {
        SystemRole.S1_WORKER: 0,
        SystemRole.S2_COORDINATOR: 1,
        SystemRole.S3_ALLOCATOR: 1,
        SystemRole.S3STAR_AUDITOR: 1,
        SystemRole.S4_SCANNER: 1,
        SystemRole.S5_POLICY: 1,
    }
    for role in missing_roles:
        counts[role] = 0
    # RunConfig validates 1..16 for mandatory roles, so we must pre-bypass
    # by constructing with valid values then mutate (frozen=True so can't);
    # instead, RunConfig's own validator should reject 0 for mandatory roles.
    return RunConfig(sub_agents_per_role=counts)


# Single missing role: each in MANDATORY_ROLES
@pytest.mark.parametrize("missing_role", list(MANDATORY_ROLES))
def test_single_missing_role_rejected_by_config(missing_role):
    """RunConfig itself should reject mandatory role with count=0.

    Note: This actually fails at RunConfig construction (REQ 13.4 validation),
    not at start_run. The structural check at lifecycle is for cases where
    RunConfig was constructed with all mandatory roles set, but a hypothetical
    role was unset entirely.
    """
    with pytest.raises(ConfigError):
        _build_config_with_missing([missing_role])


@pytest.mark.asyncio
async def test_lifecycle_with_valid_config(tmp_path):
    """Sanity: a valid RunConfig allows start_run to succeed."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    fake_llm = FakeLLMProvider(response="ok", latency=0.0)
    platform = await start_run(
        runs_dir=runs_dir,
        run_config=RunConfig(),
        llm_override=fake_llm,
        clock=SystemClock(),
    )
    try:
        # All 5 mandatory roles present
        for role in MANDATORY_ROLES:
            assert len(platform.systems.get(role, [])) >= 1
    finally:
        await platform.shutdown()


@pytest.mark.asyncio
async def test_lifecycle_constructs_run_dir(tmp_path):
    """REQ 10.3: Run dir is created at runs/{run_id}/events.jsonl."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    fake_llm = FakeLLMProvider(response="ok", latency=0.0)
    platform = await start_run(
        runs_dir=runs_dir,
        run_config=RunConfig(),
        llm_override=fake_llm,
        clock=SystemClock(),
    )
    try:
        assert platform.run_dir.exists()
        assert (platform.run_dir / "events.jsonl").exists()
        assert (platform.run_dir / "RUNNING").exists()
    finally:
        await platform.shutdown()


@pytest.mark.asyncio
async def test_shutdown_removes_lockfile(tmp_path):
    """REQ 11.6: shutdown removes RUNNING lockfile."""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    fake_llm = FakeLLMProvider(response="ok", latency=0.0)
    platform = await start_run(
        runs_dir=runs_dir,
        run_config=RunConfig(),
        llm_override=fake_llm,
        clock=SystemClock(),
    )
    await platform.shutdown()
    assert not (platform.run_dir / "RUNNING").exists()
    # events.jsonl should still be there
    assert (platform.run_dir / "events.jsonl").exists()


# Multiple missing roles
@pytest.mark.parametrize("k", [2, 3])
def test_multiple_missing_roles_rejected(k):
    """RunConfig rejects any combination of >=k mandatory roles set to 0."""
    for combo in itertools.combinations(MANDATORY_ROLES, k):
        with pytest.raises(ConfigError):
            _build_config_with_missing(list(combo))
