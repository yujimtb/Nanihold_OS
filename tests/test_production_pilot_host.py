from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, TypeAlias

import pytest
from pydantic import BaseModel

from scripts.production_pilot_host import (
    ConflictError,
    InterfaceTurnRequest,
    ProductionPilotHost,
    ReceiptStore,
    ReorientationTurnRequest,
)


CERTIFICATE = "a" * 64
RequestModel: TypeAlias = type[InterfaceTurnRequest] | type[ReorientationTurnRequest]
CLAUDE_CANDIDATE = {
    "adapter": "claude-code",
    "adapter_version": "2.1.215",
    "provider": "anthropic",
    "selection": "provider_configured",
    "effort": "high",
    "toolset": ["mcp__history__search"],
    "sandbox_fingerprint": "sandbox:isolated",
    "environment_fingerprint": "windows:pilot-host",
}
CODEX_CANDIDATE = {
    "adapter": "codex-cli",
    "adapter_version": "0.145.0",
    "provider": "openai",
    "selection": "exact",
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
            "request_document_directory": str(tmp_path / "request-documents"),
            "max_request_document_bytes": 32_768,
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


def _reorientation_request(
    history_value: object = "open",
) -> dict[str, object]:
    return {
        "receipt_id": "receipt:reorientation:1",
        "idempotency_key": "idem:reorientation:1",
        "device_identity": _identity(),
        "candidate": CLAUDE_CANDIDATE,
        "permission_mode": "sandboxed_bypass",
        "max_budget_usd": 1.0,
        "timeout_seconds": 30,
        "root_session_id": None,
        "fork_session": False,
        "event_delta": _event_delta(),
        "resume_pack": None,
        "objective": "Review the indexed history and identify gaps.",
        "session_index_ref": "history:index:1",
        "open_commitment_refs": ["commitment:1"],
        "current_state_ref": "history:state:1",
        "history_result": {
            "action_id": "action:history:1",
            "operation": "get_current_state",
            "result_json": [
                {
                    "state_key": "mission",
                    "value": history_value,
                }
            ],
            "result_blob_ref": f"blob:sha256:{'b' * 64}",
            "result_sha256": "c" * 64,
            "next_cursor": None,
            "source_cursor": "operational:7",
            "result_event_id": "event:history:state:1",
            "event_cursor": 7,
        },
        "assessment_contract": {
            "import_id": "history-import:primary",
            "canonical_conversation_id": "conversation:reorientation",
            "covered_session_index_ref": "history:index:one",
            "covered_session_count": 1,
            "open_commitment_ids": ["commitment:1"],
            "resume_work_items": [
                {
                    "work_item_id": "work:resume",
                    "title": "Resume real work",
                    "description": "Finish the imported incomplete WorkItem.",
                    "acceptance_criteria": ["The real WorkItem is resumed."],
                    "state": "paused",
                }
            ],
            "minimum_history_cursor": 3,
        },
        "audited_history_event_ids": [
            "event:history:session:1",
            "event:history:state:1",
        ],
        "assessment_contract_included": True,
        "session_index_event_ids": ["event:session-index:1"],
        "session_index_summary": {
            "session_count": 832,
            "source_kind_counts": {"claude": 832},
            "first_message_at": "2026-07-01T00:00:00+00:00",
            "last_message_at": "2026-07-20T00:00:00+00:00",
        },
    }


def _submit_reorientation_action() -> dict[str, object]:
    return {
        "action_id": "action:reorientation-submit",
        "kind": "reorientation.submit",
        "assessment": {
            "assessment_id": "assessment:one",
            "import_id": "history-import:primary",
            "conversation_id": "conversation:reorientation",
            "generated_at": "2026-07-20T00:00:00+00:00",
            "understanding": "Current state understood.",
            "active_missions": ["Resume the real unfinished WorkItem."],
            "decisions_and_constraints": ["Wait for owner confirmation."],
            "open_commitment_ids": ["commitment:1"],
            "unknowns": [],
            "resume_work_item_ids": ["work:resume"],
            "covered_session_index_ref": "history:index:one",
            "covered_session_count": 1,
            "history_cursor": 7,
            "current_state_cursor": 7,
            "citations": [
                {
                    "claim_ref": "understanding",
                    "evidence_ref": "event:history-state-1",
                }
            ],
        },
    }


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_request_document(
    *,
    argv: list[str],
    request_document_directory: Path,
) -> tuple[Path, bytes, dict[str, object]]:
    assert argv.count("--append-system-prompt-file") == 1
    path = Path(argv[argv.index("--append-system-prompt-file") + 1])
    assert path.parent == request_document_directory.resolve()
    assert path.suffix == ".json"
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    assert path.name == f"{digest}.json"
    assert payload.endswith(b"\n")
    document = json.loads(payload.decode("utf-8"))
    assert isinstance(document, dict)
    return path, payload, document


def _assert_request_document_identity(
    *,
    document: dict[str, object],
    request: dict[str, object],
    request_model: RequestModel,
) -> None:
    validated = request_model.model_validate(request)
    assert isinstance(validated, BaseModel)
    assert document["document_schema"] == "nanihold.interface-request-document"
    assert document["document_schema_version"] == "1.0.0"
    assert document["request_receipt_id"] == request["receipt_id"]
    assert document["request_idempotency_key"] == request["idempotency_key"]
    assert document["request_sha256"] == _canonical_sha256(
        validated.model_dump(mode="json", exclude_computed_fields=True)
    )


def _assert_short_stdio(
    *,
    run_kwargs: dict[str, object],
    request_document_sha256: str,
    forbidden_fragments: tuple[str, ...],
) -> None:
    instruction = run_kwargs["input"]
    assert isinstance(instruction, str)
    assert len(instruction.encode("utf-8")) <= 256
    assert f"Verify its SHA-256 is {request_document_sha256}." in instruction
    for fragment in forbidden_fragments:
        assert fragment not in instruction
    assert run_kwargs["creationflags"] == getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )


def _interface_request_with_document_bytes(
    host: ProductionPilotHost,
    target_bytes: int,
) -> dict[str, object]:
    request = _interface_request()
    request["owner_text"] = "x"
    validated = InterfaceTurnRequest.model_validate(request)
    payload = host.claude._request_document_payload(
        "/v1/interface-turn",
        validated,
    )
    base_bytes = len(host.claude._encode_content_addressed_document(payload))
    assert base_bytes <= target_bytes
    request["owner_text"] = "x" * (target_bytes - base_bytes + 1)
    validated = InterfaceTurnRequest.model_validate(request)
    payload = host.claude._request_document_payload(
        "/v1/interface-turn",
        validated,
    )
    assert (
        len(host.claude._encode_content_addressed_document(payload))
        == target_bytes
    )
    return request


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
    actual_model: str = "claude-haiku-4-5-20251001",
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


def test_health_declares_candidate_selection(tmp_path: Path, monkeypatch, provider_env):
    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        assert version is not None
        return version

    health = _make_host(tmp_path, monkeypatch, fake_run).health()
    assert health["candidates"]["interface"]["selection"] == "provider_configured"
    assert health["candidates"]["coding_s1"]["selection"] == "exact"
    assert health["max_request_document_bytes"] == 32_768


def test_claude_interface_uses_provider_configuration_permission_mcp_and_root_fork(
    tmp_path: Path, monkeypatch, provider_env
):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        calls.append((argv, kwargs))
        assert kwargs["shell"] is False
        assert kwargs["cwd"] == tmp_path.resolve()
        return subprocess.CompletedProcess(argv, 0, _claude_outer(), "")

    host = _make_host(tmp_path, monkeypatch, fake_run)
    request = _interface_request()
    long_owner_text = (
        "LONG_INTERFACE_OWNER_CONTEXT|"
        + "過去の会話履歴と未完了の約束を再確認してください。" * 300
    )
    request["owner_text"] = long_owner_text
    receipt = host.execute("/v1/interface-turn", request)

    assert receipt["status"] == "succeeded", receipt
    assert receipt["actual_model"] == "claude-haiku-4-5-20251001"
    assert receipt["provider_session_id"] == "fork-session-2"
    assert receipt["usage"]["classifier_triggered"] is False
    assert receipt["request_sha256"] == _canonical_sha256(
        InterfaceTurnRequest.model_validate(request).model_dump(
            mode="json", exclude_computed_fields=True
        )
    )
    argv, run_kwargs = calls[0]
    assert "--model" not in argv
    assert argv[argv.index("--effort") + 1] == "high"
    interface_schema_json = argv[argv.index("--json-schema") + 1]
    assert '"discriminator"' not in interface_schema_json
    assert "oneOf" in interface_schema_json
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
    argv_json = json.dumps(argv, ensure_ascii=False)
    assert long_owner_text not in argv_json
    assert "LONG_INTERFACE_OWNER_CONTEXT" not in argv_json
    assert request["receipt_id"] not in argv_json
    request_document_path, request_document_bytes, request_document = (
        _read_request_document(
            argv=argv,
            request_document_directory=tmp_path / "request-documents",
        )
    )
    _assert_request_document_identity(
        document=request_document,
        request=request,
        request_model=InterfaceTurnRequest,
    )
    assert request_document["endpoint"] == "/v1/interface-turn"
    assert request_document["owner_text"] == long_owner_text
    assert "history_result" not in request_document
    _assert_short_stdio(
        run_kwargs=run_kwargs,
        request_document_sha256=request_document_path.stem,
        forbidden_fragments=(
            long_owner_text,
            "LONG_INTERFACE_OWNER_CONTEXT",
            str(request["receipt_id"]),
        ),
    )
    provider_io_files = list(
        (tmp_path / "request-documents" / "provider-io").glob("*.json")
    )
    assert len(provider_io_files) == 1
    provider_io_bytes = provider_io_files[0].read_bytes()
    assert provider_io_files[0].name == (
        f"{hashlib.sha256(provider_io_bytes).hexdigest()}.json"
    )
    provider_io = json.loads(provider_io_bytes)
    assert provider_io["document_schema"] == "nanihold.provider-io-document"
    assert provider_io["request_receipt_id"] == request["receipt_id"]
    assert provider_io["request_sha256"] == receipt["request_sha256"]
    assert provider_io["request_document_sha256"] == request_document_path.stem
    assert provider_io["request_document_bytes"] == len(request_document_bytes)
    assert provider_io["endpoint"] == "/v1/interface-turn"
    assert provider_io["return_code"] == 0
    assert provider_io["stdout_sha256"] == hashlib.sha256(
        provider_io["stdout_text"].encode("utf-8")
    ).hexdigest()
    assert provider_io["stderr_bytes"] == 0


def test_request_document_just_below_limit_is_persisted_and_invoked(
    tmp_path: Path, monkeypatch, provider_env
):
    provider_calls = 0

    def fake_run(argv, **kwargs):
        nonlocal provider_calls
        version = _version_result(argv)
        if version is not None:
            return version
        provider_calls += 1
        return subprocess.CompletedProcess(argv, 0, _claude_outer(), "")

    host = _make_host(tmp_path, monkeypatch, fake_run)
    request = _interface_request_with_document_bytes(host, 32_767)
    receipt = host.execute("/v1/interface-turn", request)

    assert receipt["status"] == "succeeded", receipt
    assert provider_calls == 1
    request_documents = list((tmp_path / "request-documents").glob("*.json"))
    assert len(request_documents) == 1
    assert request_documents[0].stat().st_size == 32_767


def test_request_document_over_limit_fails_before_write_or_provider(
    tmp_path: Path, monkeypatch, provider_env
):
    provider_calls = 0

    def fake_run(argv, **kwargs):
        nonlocal provider_calls
        version = _version_result(argv)
        if version is not None:
            return version
        provider_calls += 1
        raise AssertionError("provider must not run for an oversized request document")

    host = _make_host(tmp_path, monkeypatch, fake_run)
    request = _interface_request_with_document_bytes(host, 32_769)
    receipt = host.execute("/v1/interface-turn", request)

    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "RequestDocumentTooLarge"
    assert provider_calls == 0
    assert list((tmp_path / "request-documents").glob("*.json")) == []
    assert list(
        (tmp_path / "request-documents" / "provider-io").glob("*.json")
    ) == []


@pytest.mark.parametrize("invalid_limit", [0, -1, 1.5, "32768", True])
def test_max_request_document_bytes_must_be_a_positive_integer(
    invalid_limit: object,
    tmp_path: Path,
    monkeypatch,
    provider_env,
):
    config = _config(tmp_path)
    config["claude"]["max_request_document_bytes"] = invalid_limit

    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        assert version is not None
        return version

    monkeypatch.setattr(
        "scripts.production_pilot_host.subprocess.run",
        fake_run,
    )
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        ProductionPilotHost(
            _write_config(tmp_path, config),
            tmp_path / "pilot.log",
        )


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
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        calls.append((argv, kwargs))
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
    long_history_value = (
        "LONG_REORIENTATION_HISTORY_RESULT|"
        + "履歴の根拠を文書経由でのみ受け渡します。" * 300
    )
    request = _reorientation_request(long_history_value)

    receipt = host.execute("/v1/reorientation-turn", request)

    assert receipt["error"] is None, receipt["error"]
    assert receipt["status"] == "succeeded", receipt
    assert receipt["result"]["actions"][0]["kind"] == "history.read"
    argv, run_kwargs = calls[0]
    assert "--resume" not in argv
    assert "--fork-session" not in argv
    reorientation_schema_json = argv[argv.index("--json-schema") + 1]
    assert '"discriminator"' not in reorientation_schema_json
    reorientation_schema = json.loads(reorientation_schema_json)
    actions_schema = reorientation_schema["properties"]["actions"]
    assert actions_schema["minItems"] == 1
    assert actions_schema["maxItems"] == 1
    assert actions_schema["items"]["oneOf"] == [
        {"$ref": "#/$defs/ReadHistoryAction"},
        {"$ref": "#/$defs/SubmitReorientationAction"},
    ]
    assert reorientation_schema["properties"]["display_text"]["maxLength"] == 1_200
    assessment_schema = reorientation_schema["$defs"]["ReorientationAssessment"]
    assert assessment_schema["properties"]["understanding"]["maxLength"] == 1_200
    assert assessment_schema["properties"]["active_missions"]["maxItems"] == 8
    assert (
        assessment_schema["properties"]["active_missions"]["items"]["maxLength"]
        == 500
    )
    assert (
        assessment_schema["properties"]["decisions_and_constraints"]["maxItems"]
        == 12
    )
    assert assessment_schema["properties"]["unknowns"]["maxItems"] == 8
    assert assessment_schema["properties"]["citations"]["maxItems"] == 32
    argv_json = json.dumps(argv, ensure_ascii=False)
    assert long_history_value not in argv_json
    assert "LONG_REORIENTATION_HISTORY_RESULT" not in argv_json
    assert '"state_key"' not in argv_json
    assert "history:state:1" not in json.dumps(argv)
    request_document_path, request_document_bytes, request_document = (
        _read_request_document(
            argv=argv,
            request_document_directory=tmp_path / "request-documents",
        )
    )
    _assert_request_document_identity(
        document=request_document,
        request=request,
        request_model=ReorientationTurnRequest,
    )
    assert request_document["endpoint"] == "/v1/reorientation-turn"
    assert request_document["reorientation_only"] is True
    assert "owner_text" not in request_document
    assert request_document["history_result"] == request["history_result"]
    assert request_document["history_result"]["result_json"] == [
        {
            "state_key": "mission",
            "value": long_history_value,
        }
    ]
    assert (
        "paginated index without value bodies"
        in request_document["assessment_submission_contract"]
    )
    assert (
        "exact state_key from the index"
        in request_document["assessment_submission_contract"]
    )
    assert (
        "Never request that same triple again"
        in request_document["assessment_submission_contract"]
    )
    assert (
        "never pass them to resolve_reference"
        in request_document["assessment_submission_contract"]
    )
    assert (
        request_document["history_result"]["result_event_id"]
        == "event:history:state:1"
    )
    assert request_document["assessment_contract"] == request["assessment_contract"]
    assert (
        request_document["audited_history_event_ids"]
        == request["audited_history_event_ids"]
    )
    assert long_history_value.encode("utf-8") in request_document_bytes
    _assert_short_stdio(
        run_kwargs=run_kwargs,
        request_document_sha256=request_document_path.stem,
        forbidden_fragments=(
            long_history_value,
            "LONG_REORIENTATION_HISTORY_RESULT",
            '"history_result"',
            str(request["receipt_id"]),
        ),
    )

    continuation = dict(request)
    continuation.update(
        {
            "receipt_id": "receipt:reorientation:2",
            "idempotency_key": "idem:reorientation:2",
            "root_session_id": "provider-leaf-1",
            "fork_session": True,
            "assessment_contract": {
                "import_id": "history-import:primary",
                "canonical_conversation_id": "conversation:reorientation",
                "contract_sha256": "d" * 64,
                "covered_session_index_ref": "history:index:one",
                "covered_session_count": 1,
                "open_commitment_ids": ["commitment:1"],
                "resume_work_items": [
                    {
                        "work_item_id": "work:resume",
                        "title": "Resume real work",
                        "description": "Finish the imported incomplete WorkItem.",
                        "acceptance_criteria": ["The real WorkItem is resumed."],
                        "state": "paused",
                    }
                ],
                "minimum_history_cursor": 3,
            },
            "assessment_contract_included": False,
        }
    )
    host.execute("/v1/reorientation-turn", continuation)
    continuation_argv, continuation_kwargs = calls[1]
    continuation_argv_json = json.dumps(continuation_argv, ensure_ascii=False)
    assert long_history_value not in continuation_argv_json
    assert "LONG_REORIENTATION_HISTORY_RESULT" not in continuation_argv_json
    continuation_document_path, _, continuation_document = _read_request_document(
        argv=continuation_argv,
        request_document_directory=tmp_path / "request-documents",
    )
    _assert_request_document_identity(
        document=continuation_document,
        request=continuation,
        request_model=ReorientationTurnRequest,
    )
    assert continuation_document["assessment_contract_included"] is False
    assert set(continuation_document["assessment_contract"]) == {
        "import_id",
        "canonical_conversation_id",
        "contract_sha256",
        "covered_session_index_ref",
        "covered_session_count",
        "open_commitment_ids",
        "resume_work_items",
        "minimum_history_cursor",
    }
    assert continuation_document["assessment_contract"]["resume_work_items"] == [
        {
            "work_item_id": "work:resume",
            "title": "Resume real work",
            "description": "Finish the imported incomplete WorkItem.",
            "acceptance_criteria": ["The real WorkItem is resumed."],
            "state": "paused",
        }
    ]
    assert continuation_document["history_result"] == continuation["history_result"]
    assert "covered_session_ids" not in json.dumps(continuation_document)
    assert "history-session:one" not in json.dumps(continuation_document)
    assert "history-session:one" not in json.dumps(continuation_argv)
    _assert_short_stdio(
        run_kwargs=continuation_kwargs,
        request_document_sha256=continuation_document_path.stem,
        forbidden_fragments=(
            long_history_value,
            "LONG_REORIENTATION_HISTORY_RESULT",
            '"history_result"',
            str(continuation["receipt_id"]),
        ),
    )


def test_reorientation_accepts_exactly_one_submit_action(
    tmp_path: Path, monkeypatch, provider_env
):
    def fake_run(argv, **kwargs):
        version = _version_result(argv)
        if version is not None:
            return version
        return subprocess.CompletedProcess(
            argv,
            0,
            _claude_outer(actions=[_submit_reorientation_action()]),
            "",
        )

    host = _make_host(tmp_path, monkeypatch, fake_run)
    receipt = host.execute(
        "/v1/reorientation-turn",
        _reorientation_request(),
    )

    assert receipt["status"] == "succeeded", receipt
    assert receipt["error"] is None
    assert len(receipt["result"]["actions"]) == 1
    assert receipt["result"]["actions"][0]["kind"] == "reorientation.submit"
    assert (
        receipt["result"]["actions"][0]["assessment"]["assessment_id"]
        == "assessment:one"
    )


@pytest.mark.parametrize(
    "actions",
    [
        [],
        [
            {
                "action_id": "action:history:one",
                "kind": "history.read",
                "operation": "search",
                "argument": "first",
                "page_cursor": None,
            },
            {
                "action_id": "action:history:two",
                "kind": "history.read",
                "operation": "search",
                "argument": "second",
                "page_cursor": None,
            },
        ],
        [
            {
                "action_id": "action:decision:forbidden",
                "kind": "decision.record",
                "statement": "This action is outside reorientation.",
                "supersedes_decision_id": None,
            }
        ],
    ],
    ids=["zero-actions", "two-actions", "other-action"],
)
def test_reorientation_rejects_action_count_and_type_outside_exact_contract(
    actions: list[dict[str, object]],
    tmp_path: Path,
    monkeypatch,
    provider_env,
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
            _claude_outer(actions=actions),
            "",
        )

    host = _make_host(tmp_path, monkeypatch, fake_run)
    receipt = host.execute(
        "/v1/reorientation-turn",
        _reorientation_request(),
    )

    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "ProviderProtocolError"
    assert "reorientation schema" in receipt["error"]["message"]
    assert provider_calls == 1


def test_provider_configured_interface_records_actual_model_evidence(
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
    assert first["status"] == "succeeded"
    assert first["actual_model"] == "claude-opus-4-6"
    assert first["error"] is None
    assert first["usage"]["model_substitution"] is False
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
        requested_model=None,
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
