from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.production_pilot_host import (
    ConflictError,
    ProductionPilotHost,
    ReceiptStore,
)


CERTIFICATE = "a" * 64
CLAUDE_CANDIDATE = {
    "adapter": "claude-code",
    "adapter_version": "2.1.215",
    "provider": "anthropic",
    "model_snapshot": "claude-fable-5",
    "effort": "high",
    "toolset": ["mcp__history__search"],
    "sandbox_fingerprint": "sandbox:isolated",
    "environment_fingerprint": "windows:pilot-host",
}
CODEX_CANDIDATE = {
    "adapter": "codex-cli",
    "adapter_version": "0.145.0",
    "provider": "openai",
    "model_snapshot": "gpt-5.6-sol",
    "effort": "xhigh",
    "toolset": ["mcp__gateway__git_status"],
    "sandbox_fingerprint": "sandbox:workspace-write",
    "environment_fingerprint": "windows:pilot-host",
}


def _config(tmp_path: Path) -> dict[str, Any]:
    return {
        "pilot_host_id": "pilot-host:production",
        "device_id": "device:production",
        "device_certificate_sha256": CERTIFICATE,
        "bearer_token_env": "TEST_PILOT_BEARER",
        "bind_host": "127.0.0.1",
        "bind_port": 18181,
        "receipt_store_path": str(tmp_path / "receipts.sqlite3"),
        "claude": {
            "candidate": CLAUDE_CANDIDATE,
            "executable": "claude",
            "cli_version": "2.1.215",
            "working_directory": str(tmp_path),
            "permission_mode": "sandboxed_bypass",
            "sandbox_profile_certificate_sha256": "b" * 64,
            "mcp": {
                "allowlist": ["history"],
                "servers": {
                    "history": {
                        "url": "https://history.example.invalid/mcp",
                        "bearer_token_env_var": "TEST_HISTORY_BEARER",
                    }
                },
            },
            "max_budget_usd": 5.0,
            "timeout_seconds": 120,
        },
        "codex": {
            "candidate": CODEX_CANDIDATE,
            "executable": "codex",
            "cli_version": "0.145.0",
            "working_directory_allowlist": [str(tmp_path)],
            "sandbox": "workspace-write",
            "mcp": {
                "allowlist": ["gateway"],
                "servers": {
                    "gateway": {
                        "url": "https://gateway.example.invalid/mcp",
                        "bearer_token_env_var": "TEST_GATEWAY_BEARER",
                    }
                },
            },
            "max_input_tokens": 10_000,
            "max_output_tokens": 2_000,
            "max_total_tokens": 12_000,
            "timeout_seconds": 300,
        },
    }


def _write_config(tmp_path: Path, value: dict[str, Any]) -> Path:
    path = tmp_path / "pilot-host.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _identity() -> dict[str, str]:
    return {
        "pilot_host_id": "pilot-host:production",
        "device_id": "device:production",
        "certificate_sha256": CERTIFICATE,
    }


def _event_delta() -> dict[str, object]:
    return {
        "after_cursor": 12,
        "through_cursor": 12,
        "event_count": 0,
        "event_type_counts": {},
        "changed_stream_ids": [],
    }


def _interface_request() -> dict[str, object]:
    return {
        "receipt_id": "receipt:interface:1",
        "idempotency_key": "idem:interface:1",
        "device_identity": _identity(),
        "candidate": CLAUDE_CANDIDATE,
        "permission_mode": "sandboxed_bypass",
        "max_budget_usd": 1.25,
        "timeout_seconds": 30,
        "root_session_id": "root-session-1",
        "fork_session": True,
        "event_delta": _event_delta(),
        "resume_pack": None,
        "owner_text": "進捗を確認してください",
    }


def _work_request(tmp_path: Path) -> dict[str, object]:
    return {
        "receipt_id": "receipt:work:1",
        "idempotency_key": "idem:work:1",
        "device_identity": _identity(),
        "candidate": CODEX_CANDIDATE,
        "execution_id": "execution:1",
        "work_item": {
            "work_item_id": "work:1",
            "title": "PilotHost test",
            "objective": "Implement the bounded change and run its tests.",
        },
        "unmet_acceptance": ["targeted tests pass"],
        "event_delta": _event_delta(),
        "artifact_refs": [
            {
                "artifact_id": "artifact:spec",
                "sha256": "c" * 64,
                "media_type": "text/markdown",
            }
        ],
        "cwd": str(tmp_path),
        "sandbox": "workspace-write",
        "token_budget": {
            "max_input_tokens": 1_000,
            "max_output_tokens": 500,
            "max_total_tokens": 1_500,
        },
        "timeout_seconds": 60,
    }


@pytest.fixture
def provider_env(monkeypatch):
    monkeypatch.setenv("TEST_PILOT_BEARER", "pilot-secret")
    monkeypatch.setenv("TEST_HISTORY_BEARER", "history-secret")
    monkeypatch.setenv("TEST_GATEWAY_BEARER", "gateway-secret")


def _version_result(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    if argv == ["claude", "--version"]:
        return subprocess.CompletedProcess(argv, 0, "2.1.215 (Claude Code)\n", "")
    if argv == ["codex", "--version"]:
        return subprocess.CompletedProcess(argv, 0, "codex-cli 0.145.0\n", "")
    return None


def _claude_outer(
    actual_model: str = "claude-fable-5",
    actions: list[dict[str, object]] | None = None,
    permission_denials: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "session_id": "fork-session-2",
            "duration_ms": 321,
            "permission_denials": (
                [] if permission_denials is None else permission_denials
            ),
            "modelUsage": {
                actual_model: {
                    "inputTokens": 100,
                    "cacheCreationInputTokens": 10,
                    "cacheReadInputTokens": 50,
                    "outputTokens": 20,
                    "costUSD": 0.25,
                }
            },
            "structured_output": {
                "display_text": "状況を確認しました。",
                "actions": [] if actions is None else actions,
            },
        }
    )


def _make_host(
    tmp_path: Path,
    monkeypatch,
    runner,
    config: dict[str, Any] | None = None,
) -> ProductionPilotHost:
    monkeypatch.setattr(
        "scripts.production_pilot_host.subprocess.run",
        runner,
    )
    return ProductionPilotHost(
        _write_config(tmp_path, config or _config(tmp_path)),
        tmp_path / "pilot.log",
    )


def test_claude_fable_uses_exact_model_permission_mcp_and_root_fork(
    tmp_path: Path, monkeypatch, provider_env
):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        calls.append(argv)
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == tmp_path.resolve()
        return subprocess.CompletedProcess(argv, 0, _claude_outer(), "")

    host = _make_host(tmp_path, monkeypatch, fake_run)
    receipt = host.execute("/v1/interface-turn", _interface_request())

    assert receipt["status"] == "succeeded", receipt
    assert receipt["actual_model"] == "claude-fable-5"
    assert receipt["provider_session_id"] == "fork-session-2"
    assert receipt["usage"]["classifier_triggered"] is False
    argv = calls[0]
    assert argv[argv.index("--model") + 1] == "claude-fable-5"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--resume") + 1] == "root-session-1"
    assert "--fork-session" in argv
    assert "--fallback-model" not in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--safe-mode" not in argv
    assert "--strict-mcp-config" in argv
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])
    assert set(mcp["mcpServers"]) == {"history"}
    assert "history-secret" not in json.dumps(argv)
    assert "pilot-secret" not in json.dumps(argv)


def test_codex_exec_has_exact_model_effort_cwd_sandbox_and_schema(
    tmp_path: Path, monkeypatch, provider_env
):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        calls.append(argv)
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        assert schema_path.is_file()
        schema = json.loads(schema_path.read_text("utf-8"))
        assert schema["additionalProperties"] is False
        output_path.write_text(
            json.dumps(
                {
                    "summary": "Implemented and tested.",
                    "acceptance_results": [
                        {
                            "criterion": "targeted tests pass",
                            "satisfied": True,
                            "evidence_refs": ["artifact:test-log"],
                        }
                    ],
                    "artifact_refs": ["artifact:commit"],
                    "event_notes": ["tests passed"],
                    "completed": True,
                }
            ),
            encoding="utf-8",
        )
        stdout = "\n".join(
            (
                json.dumps(
                    {"type": "thread.started", "thread_id": "codex-thread-1"}
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "model": "gpt-5.6-sol",
                        "model_reasoning_effort": "xhigh",
                        "usage": {
                            "input_tokens": 400,
                            "cached_input_tokens": 200,
                            "output_tokens": 100,
                        },
                    }
                ),
            )
        )
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    host = _make_host(tmp_path, monkeypatch, fake_run)
    receipt = host.execute("/v1/work-executions", _work_request(tmp_path))

    assert receipt["status"] == "succeeded"
    assert receipt["actual_model"] == "gpt-5.6-sol"
    argv = calls[0]
    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="xhigh"' in argv
    assert argv[argv.index("--cd") + 1] == str(tmp_path.resolve())
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--strict-config" in argv
    assert "--ignore-user-config" in argv
    mcp_override = argv[argv.index("-c", argv.index("--ignore-user-config")) + 1]
    assert mcp_override.startswith("mcp_servers={gateway=")
    assert "history" not in mcp_override
    assert "gateway-secret" not in json.dumps(argv)
    prompt = json.loads(argv[-1])
    assert set(prompt) == {
        "contract",
        "execution_id",
        "work_item",
        "unmet_acceptance",
        "event_delta",
        "artifact_refs",
        "token_budget",
    }


def test_work_execution_rejects_arbitrary_command_before_provider(
    tmp_path: Path, monkeypatch, provider_env
):
    provider_calls = 0

    def fake_run(argv, **kwargs):
        nonlocal provider_calls
        version = _version_result(argv)
        if version is not None:
            return version
        provider_calls += 1
        raise AssertionError("provider must not run")

    host = _make_host(tmp_path, monkeypatch, fake_run)
    request = _work_request(tmp_path)
    request["command"] = "powershell -Command Remove-Item -Recurse C:\\"

    with pytest.raises(RuntimeError, match="exact endpoint contract"):
        host.execute("/v1/work-executions", request)
    assert provider_calls == 0


def test_reorientation_turn_is_read_only_and_uses_the_same_root_fork(
    tmp_path: Path, monkeypatch, provider_env
):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            _claude_outer(
                actions=[
                    {
                            "action_id": "action:history-1",
                        "kind": "history.read",
                        "operation": "search",
                        "argument": "open commitments",
                        "page_cursor": None,
                    }
                ]
            ),
            "",
        )

    host = _make_host(tmp_path, monkeypatch, fake_run)
    request = {
        "receipt_id": "receipt:reorientation:1",
        "idempotency_key": "idem:reorientation:1",
        "device_identity": _identity(),
        "candidate": CLAUDE_CANDIDATE,
        "permission_mode": "sandboxed_bypass",
        "max_budget_usd": 1.0,
        "timeout_seconds": 30,
        "root_session_id": "root-session-1",
        "fork_session": True,
        "event_delta": _event_delta(),
        "resume_pack": None,
        "objective": "Review the indexed history and identify gaps.",
        "session_index_ref": "history:index:1",
        "open_commitment_refs": ["commitment:1"],
        "current_state_ref": "history:state:1",
    }

    receipt = host.execute("/v1/reorientation-turn", request)

    assert receipt["error"] is None, receipt["error"]
    assert receipt["status"] == "succeeded", receipt
    assert receipt["result"]["actions"][0]["kind"] == "history.read"
    argv = calls[0]
    assert argv[argv.index("--resume") + 1] == "root-session-1"
    assert "--fork-session" in argv
    prompt = json.loads(argv[2])
    assert prompt["reorientation_only"] is True
    assert "owner_text" not in prompt


def test_model_mismatch_is_a_failure_receipt_and_is_not_retried(
    tmp_path: Path, monkeypatch, provider_env
):
    provider_calls = 0

    def fake_run(argv, **kwargs):
        nonlocal provider_calls
        version = _version_result(argv)
        if version is not None:
            return version
        provider_calls += 1
        return subprocess.CompletedProcess(
            argv,
            0,
            _claude_outer("claude-opus-4-6"),
            "",
        )

    host = _make_host(tmp_path, monkeypatch, fake_run)
    request = _interface_request()
    first = host.execute("/v1/interface-turn", request)
    second = host.execute("/v1/interface-turn", request)

    assert first == second
    assert first["status"] == "failed"
    assert first["actual_model"] == "claude-opus-4-6"
    assert first["error"]["code"] == "RequestedActualModelMismatch"
    assert first["usage"]["model_substitution"] is True
    assert provider_calls == 1
    assert host.receipts.get("receipt:interface:1") == first


def test_sandboxed_bypass_rejects_any_permission_denial(
    tmp_path: Path, monkeypatch, provider_env
):
    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        return subprocess.CompletedProcess(
            argv,
            0,
            _claude_outer(
                permission_denials=[
                    {
                        "tool_name": "mcp__history__search",
                        "reason": "classifier rejection",
                    }
                ]
            ),
            "",
        )

    host = _make_host(tmp_path, monkeypatch, fake_run)
    receipt = host.execute("/v1/interface-turn", _interface_request())

    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "ClassifierUnexpected"
    assert receipt["usage"]["permission_rejections"] == 1


def test_receipt_store_marks_interrupted_invocation_transport_unknown(
    tmp_path: Path,
):
    path = tmp_path / "receipts.sqlite3"
    store = ReceiptStore(path)
    store.begin(
        endpoint="/v1/interface-turn",
        receipt_id="receipt:unknown",
        idempotency_key="idem:unknown",
        request_sha256="d" * 64,
        candidate_key="candidate:key",
        requested_model="claude-fable-5",
    )

    recovered = ReceiptStore(path).get("receipt:unknown")

    assert recovered is not None
    assert recovered["status"] == "transport_unknown"
    assert recovered["error"]["code"] == "TransportUnknown"


def test_idempotency_key_cannot_be_reused_for_different_request(
    tmp_path: Path, monkeypatch, provider_env
):
    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        return subprocess.CompletedProcess(argv, 0, _claude_outer(), "")

    host = _make_host(tmp_path, monkeypatch, fake_run)
    host.execute("/v1/interface-turn", _interface_request())
    changed = _interface_request()
    changed["receipt_id"] = "receipt:interface:other"
    changed["owner_text"] = "different"

    with pytest.raises(ConflictError):
        host.execute("/v1/interface-turn", changed)


def test_auth_requires_token_and_exact_device_headers(
    tmp_path: Path, monkeypatch, provider_env
):
    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        raise AssertionError("provider must not run")

    host = _make_host(tmp_path, monkeypatch, fake_run)
    headers = {
        "Authorization": "Bearer pilot-secret",
        "X-Nanihold-Pilot-Host-Id": "pilot-host:production",
        "X-Nanihold-Device-Id": "device:production",
        "X-Nanihold-Device-Certificate-Sha256": CERTIFICATE,
    }

    assert host.authorized(headers) is True
    headers["X-Nanihold-Device-Id"] = "device:other"
    assert host.authorized(headers) is False


def test_config_forbids_fallback_model_field(
    tmp_path: Path, monkeypatch, provider_env
):
    config = _config(tmp_path)
    config["claude"]["fallback_model"] = "claude-opus-4-6"

    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        raise AssertionError("provider must not run")

    monkeypatch.setattr(
        "scripts.production_pilot_host.subprocess.run",
        fake_run,
    )
    with pytest.raises(RuntimeError, match="exact contract"):
        ProductionPilotHost(
            _write_config(tmp_path, config),
            tmp_path / "pilot.log",
        )
