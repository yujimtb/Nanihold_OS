from __future__ import annotations

import json
import subprocess

from scripts.local_pilot_host import PilotHost


def test_local_pilot_host_uses_empty_strict_mcp_and_reports_exact_usage(
    tmp_path, monkeypatch
):
    candidate = {
        "adapter": "claude-code",
        "adapter_version": "2.1.215",
        "provider": "anthropic",
        "model_snapshot": "claude-haiku-4-5-20251001",
        "effort": "low",
        "toolset": ["conversation-only"],
        "sandbox_fingerprint": "observe-only:no-tools",
        "environment_fingerprint": "sha256:test",
    }
    config = {
        "candidate": candidate,
        "cli_executable": "claude.exe",
        "cli_version": "2.1.215",
        "bearer_token_env": "PILOT_TEST_TOKEN",
        "max_budget_usd": 0.05,
        "timeout_seconds": 120,
        "bind_host": "127.0.0.1",
        "bind_port": 50000,
    }
    config_path = tmp_path / "pilot.json"
    config_path.write_text(json.dumps(config), "utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("PILOT_TEST_TOKEN", "secret")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(
                argv, 0, "2.1.215 (Claude Code)\n", ""
            )
        outer = {
            "modelUsage": {
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 10,
                    "cacheCreationInputTokens": 20,
                    "cacheReadInputTokens": 30,
                    "outputTokens": 5,
                    "costUSD": 0.001,
                }
            },
            "duration_ms": 100,
            "structured_output": {
                "display_text": "確認",
                "work_directives": [],
                "decisions": [],
                "commitment_updates": [],
            },
            "session_id": "provider-session",
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(outer), "")

    monkeypatch.setattr("scripts.local_pilot_host.subprocess.run", fake_run)
    host = PilotHost(config_path, workspace, tmp_path / "pilot.log")

    result = host.invoke(
        {
            "candidate": candidate,
            "owner_text": "確認",
            "context": {"provider_session_id": None},
        }
    )

    argv = calls[-1]
    mcp_index = argv.index("--mcp-config")
    assert argv[mcp_index + 1] == '{"mcpServers":{}}'
    assert argv[argv.index("--tools") + 1] == ""
    assert result["actual_model_snapshot"] == candidate["model_snapshot"]
    usage = result["structured_response"]["pilot_usage"]
    assert usage["cache_read_input_tokens"] == 30
    assert usage["cost_usd"] == 0.001
