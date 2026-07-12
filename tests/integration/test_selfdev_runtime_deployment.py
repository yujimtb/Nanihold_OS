"""実ランタイム設定から selfdev 配備を起動する統合テスト。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from vsm.config import AgentBackendConfig, AgentsConfig, LLMConfig, RunConfig, SelfDevConfig
from vsm.roles import SystemRole
from vsm.agents.backends.claude_code import ClaudeCodeRuntime
from vsm.agents.backends.codex import CodexRuntime
from vsm.web.app import create_app
from vsm.web.selfdev_runtime import build_selfdev_service


def _run_config(repository: Path) -> RunConfig:
    roles = {role: "claude-code" for role in SystemRole}
    roles[SystemRole.S1_WORKER] = "codex"
    backends = {
        "claude-code": AgentBackendConfig(
            bin="claude",
            model="claude-test-model",
            timeout_seconds=30.0,
        ),
        "codex": AgentBackendConfig(
            bin="codex",
            model="codex-test-model",
            reasoning_effort="high",
            timeout_seconds=30.0,
        ),
        "litellm": AgentBackendConfig(bin=None, model="", timeout_seconds=30.0),
        "fake": AgentBackendConfig(bin=None, model="fake/test-model", timeout_seconds=30.0),
    }
    return RunConfig(
        agents=AgentsConfig(
            default_backend="claude-code",
            backends=backends,
            roles=roles,
        ),
        selfdev=SelfDevConfig(enabled=True, repository=repository),
    )


def test_real_cli_runtime_selfdev_deployment_starts_with_fastapi_lifespan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Claude/Codex の実 runtime を配線しても controller startup が失敗しない。"""

    monkeypatch.chdir(tmp_path)
    process_factory = Mock(name="process_factory")
    service = build_selfdev_service(
        config=(LLMConfig(), _run_config(tmp_path)),
        process_factory=process_factory,
    )

    assert service is not None
    controller = service.controller
    assert isinstance(controller.implementation_runner.runtime, CodexRuntime)
    assert isinstance(controller.audit_runner.runtime, ClaudeCodeRuntime)
    assert not hasattr(controller.audit_runner.runtime, "session_ref")
    assert all(
        runtime._process_factory is process_factory
        for runtime in (
            controller.implementation_runner.runtime,
            controller.audit_runner.runtime,
            *controller.consortium.runtimes.values(),
        )
    )

    with TestClient(create_app(service)) as client:
        health = client.get("/api/selfdev/health")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["controller"] == "running"
    process_factory.assert_not_called()
