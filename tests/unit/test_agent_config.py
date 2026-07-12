"""AgentRuntime 設定とロール解決のテスト。"""

from __future__ import annotations

import pytest

from vsm.agents.backends.fake import FakeAgentRuntime
from vsm.agents.backends.claude_code import ClaudeCodeRuntime
from vsm.agents.backends.codex import CodexRuntime
from vsm.config import (
    AgentsConfig,
    LLMConfig,
    NANIHOLD_USE_FAKE_LLM_ENV,
    RunConfig,
    load_config,
)
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform, _resolve_role_runtimes


def test_agents_and_session_toml_with_bin_env_override(tmp_path, monkeypatch) -> None:
    path = tmp_path / "vsm.toml"
    path.write_text(
        """
[agents]
default_backend = "codex"
[agents.backends.claude-code]
bin = "claude-file"
model = "claude-model"
timeout_seconds = 901
[agents.backends.codex]
bin = "codex-file"
model = "gpt-test"
reasoning_effort = "medium"
timeout_seconds = 902
[agents.roles]
S5_POLICY = "codex"
S3_ALLOCATOR = ""
[session]
resume_within_run = false
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_BIN", "claude-env")
    monkeypatch.setenv("CODEX_BIN", "codex-env")

    _, run_config = load_config(path)

    assert run_config.agents.backends["claude-code"].bin == "claude-env"
    assert run_config.agents.backends["codex"].bin == "codex-env"
    assert run_config.agents.backend_for(SystemRole.S5_POLICY) == "codex"
    assert run_config.agents.backend_for(SystemRole.S3_ALLOCATOR) is None
    assert run_config.session.resume_within_run is False


def test_role_runtime_resolution_uses_distinct_instances() -> None:
    roles = {role: "fake" for role in SystemRole}
    roles[SystemRole.S3_ALLOCATOR] = ""
    config = RunConfig(agents=AgentsConfig(default_backend="fake", roles=roles))

    runtimes = _resolve_role_runtimes(
        run_config=config,
        llm_config=LLMConfig(),  # fake backend では参照されない
        llm_override=None,
        runtime_overrides=None,
    )

    assert runtimes[SystemRole.S3_ALLOCATOR] is None
    assigned = [runtime for runtime in runtimes.values() if runtime is not None]
    assert all(isinstance(runtime, FakeAgentRuntime) for runtime in assigned)
    assert len({id(runtime) for runtime in assigned}) == len(assigned)


def test_agents_roles_select_cli_backends_without_litellm_provider() -> None:
    roles = {role: "claude-code" for role in SystemRole}
    roles[SystemRole.S3_ALLOCATOR] = ""
    roles[SystemRole.S1_WORKER] = "codex"
    config = RunConfig(agents=AgentsConfig(default_backend="claude-code", roles=roles))

    runtimes = _resolve_role_runtimes(
        run_config=config,
        llm_config=LLMConfig(),
        llm_override=None,
        runtime_overrides=None,
    )

    assert isinstance(runtimes[SystemRole.S5_POLICY], ClaudeCodeRuntime)
    assert isinstance(runtimes[SystemRole.S1_WORKER], CodexRuntime)
    assert runtimes[SystemRole.S3_ALLOCATOR] is None


def test_fake_environment_is_an_explicit_runtime_selection(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(NANIHOLD_USE_FAKE_LLM_ENV, "1")

    _, config = load_config(None)

    assert config.agents.backend_for(SystemRole.S5_POLICY) == "fake"
    assert config.agents.backend_for(SystemRole.S1_WORKER) == "fake"
    assert config.agents.backend_for(SystemRole.S3_ALLOCATOR) is None

    runtimes = _resolve_role_runtimes(
        run_config=config,
        llm_config=LLMConfig(),
        llm_override=None,
        runtime_overrides=None,
    )
    assert isinstance(runtimes[SystemRole.S5_POLICY], FakeAgentRuntime)


@pytest.mark.asyncio
async def test_platform_injects_role_runtime_into_each_system(tmp_path) -> None:
    roles = {role: "fake" for role in SystemRole}
    roles[SystemRole.S3_ALLOCATOR] = ""
    config = RunConfig(agents=AgentsConfig(default_backend="fake", roles=roles))

    platform = await Platform.create(
        run_id="run-agent-role-resolution",
        runs_dir=tmp_path,
        run_config=config,
    )
    try:
        for role, systems in platform.systems.items():
            assert systems[0]._runtime is platform.runtimes[role]
        assert platform.runtimes[SystemRole.S3_ALLOCATOR] is None
    finally:
        await platform.shutdown()
