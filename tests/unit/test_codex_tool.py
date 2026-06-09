from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from vsm.authority import ParentAuthority
from vsm.tools import CodexRunFacade, CodexRunPolicy, CodexRunRequest, ToolEffect


@dataclass
class _FakeProcess:
    returncode: int = 0
    killed: bool = False

    async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
        return b"stdout", b"stderr"

    def kill(self) -> None:
        self.killed = True


class _ProcessFactory:
    def __init__(self) -> None:
        self.argv: tuple[Any, ...] | None = None
        self.kwargs: dict[str, Any] | None = None
        self.process = _FakeProcess()

    async def __call__(self, *argv: Any, **kwargs: Any) -> _FakeProcess:
        self.argv = argv
        self.kwargs = kwargs
        last_path = Path(argv[argv.index("--output-last-message") + 1])
        last_path.write_text("last message", encoding="utf-8")
        return self.process


def _authority(tmp_path: Path, effects: frozenset[ToolEffect] | None = None) -> ParentAuthority:
    return ParentAuthority(
        authority_id="auth-codex",
        issuer_node_id="parent",
        subject_node_id="node-1",
        issued_at=datetime.now(timezone.utc),
        allowed_tool_classes=effects
        or frozenset({ToolEffect.EXTERNAL_READ, ToolEffect.EXTERNAL_WRITE, ToolEffect.CONTROL}),
        filesystem_scope=(str(tmp_path),),
    )


@pytest.mark.asyncio
async def test_codex_run_workspace_write_external_write_returns_invocation_and_result(tmp_path: Path) -> None:
    factory = _ProcessFactory()
    facade = CodexRunFacade(process_factory=factory)

    invocation, result = await facade.run(
        CodexRunRequest(
            codex_key="codex-1",
            prompt="implement this",
            requested_by="node-1",
            workdir=tmp_path,
            codex_bin="codex",
            sandbox="workspace-write",
            effect=ToolEffect.EXTERNAL_WRITE,
        ),
        _authority(tmp_path),
    )

    assert invocation.tool_name == "codex_run"
    assert invocation.effect is ToolEffect.EXTERNAL_WRITE
    assert invocation.idempotency_key == "codex-1"
    assert invocation.payload["sandbox"] == "workspace-write"
    assert result.returncode == 0
    assert result.stdout == "stdout"
    assert result.stderr == "stderr"
    assert result.last_message == "last message"
    assert factory.argv is not None
    assert factory.argv[:6] == (
        "codex",
        "exec",
        "--cd",
        str(tmp_path.resolve(strict=False)),
        "--sandbox",
        "workspace-write",
    )
    assert factory.kwargs is not None
    assert factory.kwargs["cwd"] == tmp_path.resolve(strict=False)


@pytest.mark.asyncio
async def test_codex_run_read_only_external_read_is_allowed(tmp_path: Path) -> None:
    facade = CodexRunFacade(process_factory=_ProcessFactory())

    invocation, result = await facade.run(
        CodexRunRequest(
            codex_key="codex-read",
            prompt="inspect only",
            requested_by="node-1",
            workdir=tmp_path,
            sandbox="read-only",
            effect=ToolEffect.EXTERNAL_READ,
        ),
        _authority(tmp_path),
    )

    assert invocation.effect is ToolEffect.EXTERNAL_READ
    assert invocation.idempotency_key is None
    assert result.last_message == "last message"


@pytest.mark.asyncio
async def test_codex_run_rejects_denied_sandbox(tmp_path: Path) -> None:
    facade = CodexRunFacade(process_factory=_ProcessFactory())

    with pytest.raises(PermissionError, match="sandbox is denied"):
        await facade.run(
            CodexRunRequest(
                codex_key="codex-danger",
                prompt="do work",
                requested_by="node-1",
                workdir=tmp_path,
                sandbox="danger-full-access",
                effect=ToolEffect.EXTERNAL_WRITE,
            ),
            _authority(tmp_path),
        )


@pytest.mark.asyncio
async def test_codex_run_rejects_unsupported_effect(tmp_path: Path) -> None:
    facade = CodexRunFacade(process_factory=_ProcessFactory())

    with pytest.raises(PermissionError, match="effect is not supported"):
        await facade.run(
            CodexRunRequest(
                codex_key="codex-local",
                prompt="do work",
                requested_by="node-1",
                workdir=tmp_path,
                sandbox="workspace-write",
                effect=ToolEffect.LOCAL_WRITE,
            ),
            _authority(tmp_path, effects=frozenset({ToolEffect.LOCAL_WRITE})),
        )


@pytest.mark.asyncio
async def test_codex_run_rejects_workdir_outside_filesystem_scope(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    scoped = tmp_path / "scoped"
    scoped.mkdir()
    facade = CodexRunFacade(process_factory=_ProcessFactory())

    with pytest.raises(PermissionError, match="outside authority filesystem_scope"):
        await facade.run(
            CodexRunRequest(
                codex_key="codex-outside",
                prompt="do work",
                requested_by="node-1",
                workdir=outside,
                sandbox="workspace-write",
                effect=ToolEffect.EXTERNAL_WRITE,
            ),
            _authority(scoped),
        )


def test_codex_run_write_and_control_require_codex_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="codex_key is required"):
        CodexRunRequest(
            codex_key="",
            prompt="do work",
            requested_by="node-1",
            workdir=tmp_path,
            effect=ToolEffect.EXTERNAL_WRITE,
        )

    request = CodexRunRequest(
        codex_key="codex-control",
        prompt="do work",
        requested_by="node-1",
        workdir=tmp_path,
        effect=ToolEffect.CONTROL,
    )
    assert request.codex_key == "codex-control"


@pytest.mark.asyncio
async def test_danger_full_access_requires_explicit_policy(tmp_path: Path) -> None:
    facade = CodexRunFacade(process_factory=_ProcessFactory())

    invocation, _ = await facade.run(
        CodexRunRequest(
            codex_key="codex-danger-explicit",
            prompt="do work",
            requested_by="node-1",
            workdir=tmp_path,
            sandbox="danger-full-access",
            effect=ToolEffect.CONTROL,
        ),
        _authority(tmp_path),
        CodexRunPolicy(allowed_sandboxes=frozenset({"danger-full-access"})),
    )

    assert invocation.effect is ToolEffect.CONTROL
    assert invocation.idempotency_key == "codex-danger-explicit"
