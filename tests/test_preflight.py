from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vsm.environment import EnvironmentContract, environment_fingerprint
from vsm.preflight import (
    CliVersionReader,
    PreflightContractError,
    PreflightGate,
    PreflightObservation,
    VersionReadError,
    candidate_metadata_updater,
)
from scripts.production_pilot_host import (
    ContractError,
    ProductionPilotHost,
    _effective_preflight_config,
)


def _write_version(path: Path, version: str, *, mtime_ns: int | None = None) -> None:
    path.write_text(json.dumps({"name": "codex", "version": version}), encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


CONTRACT = EnvironmentContract(
    supported_shells=("posix",),
    workspace_writable=True,
    minimum_memory_mb=1,
    supported_sandboxes=("workspace-write", "read-only"),
    required_sandbox="workspace-write",
    path_mapping_names=("workspace-root",),
    adapters={
        "codex-cli": {
            "required_endpoints": ("api.openai.com",),
            "minimum_cli_version": "0.144.5",
        },
        "claude-code": {
            "required_endpoints": ("api.anthropic.com",),
            "minimum_cli_version": "2.1.215",
        },
    },
)
CONTRACT_FINGERPRINT = environment_fingerprint(CONTRACT)


def _observation(
    *,
    sandbox_policy: str = "workspace-write",
    workspace_writable: bool = True,
    shell: str = "posix",
    endpoint: str = "api.openai.com",
    rollout_ref: str | None = None,
) -> dict[str, object]:
    return {
        "sandbox_policy": sandbox_policy,
        "workspace_writable": workspace_writable,
        "endpoint_reachable": [endpoint],
        "memory_bytes": 2 * 1024 * 1024,
        "shell": shell,
        "path_mappings": ["workspace-root"],
        "rollout_ref": rollout_ref,
    }


def _gate(
    tmp_path: Path,
    *,
    runner,
    declaration: dict[str, object] | None = None,
    evidence_hook=None,
    event_hook=None,
    version: str = "0.145.0",
    contract: EnvironmentContract = CONTRACT,
):
    version_file = tmp_path / "node_modules" / "codex" / "package.json"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    if not version_file.exists():
        _write_version(version_file, version)
    claude_version_file = tmp_path / "node_modules" / "claude" / "package.json"
    claude_version_file.parent.mkdir(parents=True, exist_ok=True)
    if not claude_version_file.exists():
        _write_version(claude_version_file, "2.1.215")
    return PreflightGate(
        contract=contract,
        instance_fingerprint="instance:test",
        version_readers={
            "codex-cli": CliVersionReader(version_file),
            "claude-code": CliVersionReader(claude_version_file),
        },
        cache_path=tmp_path / "state" / "preflight.json",
        preflight_runners={"codex-cli": runner, "claude-code": runner},
        candidate_declarations=(
            None if declaration is None else {"codex-cli": declaration}
        ),
        declaration_event_hook=event_hook,
        evidence_hook=evidence_hook,
        clock=lambda: datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
    ), version_file


def test_preflight_rejects_powershell_for_posix_only_contract(tmp_path: Path):
    gate, _ = _gate(
        tmp_path,
        runner=lambda _value: _observation(shell="powershell"),
    )

    with pytest.raises(PreflightContractError, match="required shell capability"):
        gate.dispatch_preflight("codex-cli")


def test_preflight_accepts_any_supported_shell(tmp_path: Path):
    contract = CONTRACT.model_copy(update={"supported_shells": ("powershell", "posix")})
    gate, _ = _gate(
        tmp_path,
        contract=contract,
        runner=lambda _value: _observation(shell="powershell"),
    )

    assert gate.dispatch_preflight("codex-cli").cache_hit is False


def test_preflight_validates_target_adapter_requirement(tmp_path: Path):
    gate, _ = _gate(
        tmp_path,
        runner=lambda value: _observation(
            endpoint="api.anthropic.com"
            if value.adapter == "claude-code"
            else "api.openai.com"
        ),
    )

    result = gate.dispatch_preflight("claude-code")

    assert result.cache_hit is False
    assert result.evidence.verification_tuple.adapter == "claude-code"


def test_preflight_rejects_target_adapter_endpoint_and_version(tmp_path: Path):
    gate, _ = _gate(
        tmp_path,
        runner=lambda _value: _observation(),
    )

    with pytest.raises(PreflightContractError, match="required endpoints"):
        gate.dispatch_preflight("claude-code")

    low_version_gate, _ = _gate(
        tmp_path / "low-version",
        runner=lambda value: _observation(
            endpoint="api.anthropic.com" if value.adapter == "claude-code" else "api.openai.com"
        ),
        contract=EnvironmentContract.model_validate(
            {
                **CONTRACT.model_dump(),
                "adapters": {
                    **CONTRACT.model_dump()["adapters"],
                    "claude-code": {
                        "required_endpoints": ("api.anthropic.com",),
                        "minimum_cli_version": "2.1.216",
                    },
                },
            }
        ),
    )
    with pytest.raises(PreflightContractError, match="below the contract minimum"):
        low_version_gate.dispatch_preflight("claude-code")


def test_preflight_rejects_undeclared_adapter_fail_closed(tmp_path: Path):
    gate, _ = _gate(tmp_path, runner=lambda _value: _observation())

    with pytest.raises(PreflightContractError, match="not declared"):
        gate.dispatch_preflight("future-cli")


def test_変化なしはキャッシュヒットして試走をスキップ(tmp_path: Path):
    calls: list[object] = []

    def runner(value):
        calls.append(value)
        return _observation(rollout_ref="rollout:test:1")

    gate, _ = _gate(tmp_path, runner=runner)
    assert gate.dispatch_preflight("codex-cli").cache_hit is False

    def should_not_run(_value):
        raise AssertionError("cache hit must not run Codex preflight")

    second, _ = _gate(tmp_path, runner=should_not_run)
    result = second.dispatch_preflight("codex-cli")
    assert result.cache_hit is True
    assert len(calls) == 1


def test_cli更新は最初のdispatchで試走し自動更新して新タプルを永続化(
    tmp_path: Path,
):
    calls: list[str] = []
    declaration = {"adapter_version": "0.145.0"}
    events: list[object] = []

    def runner(value):
        calls.append(value.cli_version)
        return _observation(rollout_ref=f"rollout:{value.cli_version}")

    gate, version_file = _gate(
        tmp_path,
        runner=runner,
        declaration=declaration,
        event_hook=events.append,
    )
    gate.dispatch_preflight("codex-cli")
    old_mtime = version_file.stat().st_mtime_ns
    _write_version(version_file, "0.146.0", mtime_ns=old_mtime + 1_000_000)

    result = gate.dispatch_preflight("codex-cli")
    assert result.cache_hit is False
    assert calls == ["0.145.0", "0.146.0"]
    assert declaration["adapter_version"] == "0.146.0"
    assert len(events) == 1
    assert events[-1].from_value == "0.145.0"
    assert events[-1].to_value == "0.146.0"
    persisted = json.loads((tmp_path / "state" / "preflight.json").read_text())
    assert persisted["entries"][-1]["evidence"]["verification_tuple"]["cli_version"] == (
        "0.146.0"
    )


def test_cacheはオブジェクト再生成後も有効(tmp_path: Path):
    calls = 0

    def runner(_value):
        nonlocal calls
        calls += 1
        return _observation()

    first, _ = _gate(tmp_path, runner=runner)
    first.dispatch_preflight("codex-cli")
    second, _ = _gate(
        tmp_path,
        runner=lambda _value: pytest.fail("restart must reuse durable cache"),
    )
    assert second.dispatch_preflight("codex-cli").cache_hit is True
    assert calls == 1


def test_preflight失敗はfail_fastで宣言と自動更新を変更しない(tmp_path: Path):
    declaration = {"adapter_version": "0.145.0"}
    events: list[object] = []
    gate, _ = _gate(
        tmp_path,
        runner=lambda _value: _observation(
            sandbox_policy="read-only", workspace_writable=False
        ),
        declaration=declaration,
        event_hook=events.append,
    )
    with pytest.raises(PreflightContractError):
        gate.dispatch_preflight("codex-cli")
    assert declaration == {"adapter_version": "0.145.0"}
    assert events == []
    assert not (tmp_path / "state" / "preflight.json").exists()


def test_preflightはbridged観測でsandbox検証を明示的にスキップして通過する(
    tmp_path: Path,
):
    """The win32 Codex bypass bridge cannot make codex report sandbox_policy in
    its rollout, so an adapter that reports bridged=True gets a recorded,
    explicit degraded pass on the sandbox check instead of a rejection."""

    def runner(_value):
        return PreflightObservation(
            sandbox_policy="unverified_by_bridge",
            capabilities={
                "workspace_writable": True,
                "endpoint_reachable": ["api.openai.com"],
                "memory_bytes": 2 * 1024 * 1024,
                "shell": "posix",
                "path_mappings": ["workspace-root"],
            },
            rollout_ref="rollout:bridge:1",
            bridged=True,
        )

    gate, _ = _gate(tmp_path, runner=runner)
    result = gate.dispatch_preflight("codex-cli")

    assert result.cache_hit is False
    assert result.evidence.bridged is True
    assert result.evidence.measured_sandbox_policy == "unverified_by_bridge"
    persisted = json.loads((tmp_path / "state" / "preflight.json").read_text())
    assert persisted["entries"][-1]["evidence"]["bridged"] is True


def test_preflightはbridged観測でも他の検証は必須のまま(tmp_path: Path):
    """bridged only excuses the sandbox_policy check; shell/memory/endpoint/CLI
    version/instance fingerprint verification stays mandatory."""

    def runner(_value):
        return PreflightObservation(
            sandbox_policy="unverified_by_bridge",
            capabilities={
                "workspace_writable": True,
                "endpoint_reachable": ["api.openai.com"],
                "memory_bytes": 2 * 1024 * 1024,
                "shell": "powershell",  # CONTRACT only supports posix
                "path_mappings": ["workspace-root"],
            },
            rollout_ref="rollout:bridge:2",
            bridged=True,
        )

    gate, _ = _gate(tmp_path, runner=runner)
    with pytest.raises(PreflightContractError, match="required shell capability"):
        gate.dispatch_preflight("codex-cli")


def testバージョン読み取りはプロセスを起動しない(tmp_path: Path, monkeypatch):
    version_file = tmp_path / "package.json"
    _write_version(version_file, "2.1.216")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("version reading must not start a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert CliVersionReader(version_file).read().version == "2.1.216"


def testバージョンファイルが読めなければfail_fast(tmp_path: Path):
    with pytest.raises(VersionReadError):
        CliVersionReader(tmp_path / "missing-package.json").read()


def test_kernel_config_is_authoritative_over_pilot_host_fallback(tmp_path: Path):
    kernel_path = tmp_path / "vsm.toml"
    kernel_path.write_text(
        """
[kernel.data_space]
data_space_id = "space:kernel"

[production_pilot_host]
preflight_enabled = true
preflight_cli_version_files = { codex-cli = "/kernel/codex/package.json", claude-code = "/kernel/claude/package.json" }
preflight_cache_path = "/kernel/preflight.json"
preflight_instance_fingerprint = "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
""",
        encoding="utf-8",
    )

    effective, source_path = _effective_preflight_config(
        config_path=tmp_path / "pilot-host.json",
        raw_config={
            "enabled": False,
            "kernel_config_path": str(kernel_path),
            "cli_version_files": {
                "codex-cli": "/fallback/codex/package.json",
                "claude-code": "/fallback/claude/package.json",
            },
            "cache_path": "/fallback/preflight.json",
            "instance_fingerprint": "f" * 64,
        },
    )

    assert source_path == kernel_path
    assert effective["enabled"] is True
    assert effective["cli_version_files"] == {
        "codex-cli": "/kernel/codex/package.json",
        "claude-code": "/kernel/claude/package.json",
    }
    assert effective["cache_path"] == "/kernel/preflight.json"
    assert effective["instance_fingerprint"] == "k" * 64


def test候補宣言更新フックは決定論的な監査イベントを生成する(tmp_path: Path):
    declaration = {"adapter_version": "1.0.0"}
    events: list[object] = []
    updater = candidate_metadata_updater(declaration, event_hook=events.append)
    gate, _ = _gate(
        tmp_path,
        runner=lambda _value: _observation(),
    )
    # The direct updater API is exercised independently of the gate's default
    # updater so callers can choose either injection surface.
    evidence = gate.dispatch_preflight("codex-cli").evidence
    event = updater(evidence)
    assert declaration["adapter_version"] == "0.145.0"
    assert event.field == "candidate.adapter_version"
    assert events == [event]


def test_production_pilot_hostはdispatch直前にpreflightをゲートする(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("TEST_PILOT_BEARER", "pilot-secret")
    monkeypatch.setenv("TEST_HISTORY_BEARER", "history-secret")
    monkeypatch.setenv("TEST_GATEWAY_BEARER", "gateway-secret")
    config = {
        "pilot_host_id": "pilot-host:production",
        "device_id": "device:production",
        "device_certificate_sha256": "a" * 64,
        "bearer_token_env": "TEST_PILOT_BEARER",
        "bind_host": "127.0.0.1",
        "bind_port": 18181,
        "receipt_store_path": str(tmp_path / "receipts.sqlite3"),
        "claude": {
            "candidate": {
                "adapter": "claude-code",
                "adapter_version": "2.1.215",
                "provider": "anthropic",
                "selection": "provider_configured",
                "effort": "high",
                "toolset": ["mcp__history__search"],
                "sandbox_fingerprint": "sandbox:isolated",
                "environment_fingerprint": CONTRACT_FINGERPRINT,
            },
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
            "candidate": {
                "adapter": "codex-cli",
                "adapter_version": "0.145.0",
                "provider": "openai",
                "selection": "exact",
                "model_snapshot": "gpt-5.6-sol",
                "effort": "xhigh",
                "toolset": ["mcp__gateway__git_status"],
                "sandbox_fingerprint": "sandbox:workspace-write",
                "environment_fingerprint": CONTRACT_FINGERPRINT,
            },
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
    version_file = tmp_path / "codex-package.json"
    version_file.write_text(json.dumps({"version": "0.146.0"}), encoding="utf-8")
    config["preflight"] = {
        "enabled": True,
        "cli_version_files": {
            "codex-cli": str(version_file),
            "claude-code": str(tmp_path / "claude-package.json"),
        },
        "cache_path": str(tmp_path / "preflight.json"),
        "instance_fingerprint": "c" * 64,
        "environment_contract": CONTRACT.model_dump(mode="json"),
    }
    config_path = tmp_path / "pilot-host.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    trial_calls: list[object] = []
    host = ProductionPilotHost(
        config_path,
        tmp_path / "pilot.log",
        preflight_runner=lambda value: (
            trial_calls.append(value)
            or {"sandbox_policy": "read-only", "workspace_writable": False}
        ),
    )
    request = {
        "receipt_id": "receipt:work:preflight",
        "idempotency_key": "idem:work:preflight",
        "device_identity": {
            "pilot_host_id": "pilot-host:production",
            "device_id": "device:production",
            "certificate_sha256": "a" * 64,
        },
        "candidate": config["codex"]["candidate"],
        "execution_id": "execution:preflight",
        "work_item": {
            "work_item_id": "work:preflight",
            "title": "preflight",
            "objective": "verify gate",
            "agent_name": "Sora",
        },
        "unmet_acceptance": ["gate rejects downgrade"],
        "event_delta": {
            "after_cursor": 0,
            "through_cursor": 0,
            "event_count": 0,
            "event_type_counts": {},
            "changed_stream_ids": [],
        },
        "artifact_refs": [],
        "cwd": str(tmp_path),
        "sandbox": "workspace-write",
        "token_budget": {
            "max_input_tokens": 100,
            "max_output_tokens": 100,
            "max_total_tokens": 200,
        },
        "timeout_seconds": 30,
    }
    with pytest.raises(ContractError, match="preflight rejected"):
        host.execute("/v1/work-executions", request)
    assert len(trial_calls) == 1
    assert json.loads(config_path.read_text())["codex"]["candidate"]["adapter_version"] == (
        "0.145.0"
    )
    assert not (tmp_path / "preflight.json").exists()
