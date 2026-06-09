"""Codex CLI execution tool facade."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vsm.ids import generate_uuid
from vsm.tools.model import ToolEffect, ToolInvocation

if TYPE_CHECKING:
    from vsm.authority import ParentAuthority

_CODEX_TOOL_NAME = "codex_run"
_ALLOWED_EFFECTS = frozenset(
    {
        ToolEffect.EXTERNAL_READ,
        ToolEffect.EXTERNAL_WRITE,
        ToolEffect.CONTROL,
    }
)
_DEFAULT_ALLOWED_SANDBOXES = frozenset({"read-only", "workspace-write"})


@dataclass(frozen=True)
class CodexRunPolicy:
    """Policy boundary for a single ``codex_run`` execution."""

    allowed_sandboxes: frozenset[str] = _DEFAULT_ALLOWED_SANDBOXES
    allowed_effects: frozenset[ToolEffect] = _ALLOWED_EFFECTS


@dataclass(frozen=True)
class CodexRunRequest:
    codex_key: str
    prompt: str
    requested_by: str
    workdir: Path
    codex_bin: str = "codex"
    timeout_seconds: float = 1800.0
    sandbox: str = "workspace-write"
    effect: ToolEffect = ToolEffect.EXTERNAL_WRITE

    def __post_init__(self) -> None:
        if not self.codex_key.strip():
            raise ValueError("codex_key is required")
        if not self.prompt.strip():
            raise ValueError("prompt is required")
        if not self.requested_by.strip():
            raise ValueError("requested_by is required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(self.effect, ToolEffect):
            object.__setattr__(self, "effect", ToolEffect(self.effect))


@dataclass(frozen=True)
class CodexRunResult:
    returncode: int
    elapsed_seconds: float
    stdout: str
    stderr: str
    last_message: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "elapsed_seconds": self.elapsed_seconds,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "last_message": self.last_message,
        }


class CodexRunFacade:
    """Execute Codex CLI under Tool/Authority policy control."""

    def __init__(
        self,
        process_factory: Callable[..., Any] = asyncio.create_subprocess_exec,
    ) -> None:
        self._process_factory = process_factory

    async def run(
        self,
        request: CodexRunRequest,
        authority: ParentAuthority,
        policy: CodexRunPolicy | None = None,
    ) -> tuple[ToolInvocation, CodexRunResult]:
        active_policy = policy or CodexRunPolicy()
        workdir = _resolve_workdir(request.workdir, authority)
        _assert_policy_allows(request, authority, active_policy)

        invocation = ToolInvocation(
            invocation_id=generate_uuid(),
            tool_name=_CODEX_TOOL_NAME,
            effect=request.effect,
            requested_by_node_id=request.requested_by,
            payload={
                "codex_key": request.codex_key,
                "prompt": request.prompt,
                "workdir": str(workdir),
                "codex_bin": request.codex_bin,
                "timeout_seconds": request.timeout_seconds,
                "sandbox": request.sandbox,
            },
            idempotency_key=(
                request.codex_key
                if request.effect in {ToolEffect.EXTERNAL_WRITE, ToolEffect.CONTROL}
                else None
            ),
        )
        result = await self._execute(request, workdir)
        return invocation, result

    async def _execute(self, request: CodexRunRequest, workdir: Path) -> CodexRunResult:
        started = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory(prefix="vsm-codex-run-") as tmp_dir:
            last_file = Path(tmp_dir) / "last-message.txt"
            argv = [
                request.codex_bin,
                "exec",
                "--cd",
                str(workdir),
                "--sandbox",
                request.sandbox,
                "--color",
                "never",
                "--output-last-message",
                str(last_file),
                "-",
            ]
            env = os.environ.copy()
            env.setdefault("NO_COLOR", "1")
            process = await self._process_factory(
                *argv,
                cwd=workdir,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(request.prompt.encode("utf-8")),
                    timeout=request.timeout_seconds,
                )
                returncode = process.returncode if process.returncode is not None else 1
            except asyncio.TimeoutError:
                process.kill()
                stdout_bytes, stderr_bytes = await process.communicate()
                returncode = 124

            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            return CodexRunResult(
                returncode=returncode,
                elapsed_seconds=elapsed,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                last_message=_read_text_if_exists(last_file),
            )


def _assert_policy_allows(
    request: CodexRunRequest,
    authority: ParentAuthority,
    policy: CodexRunPolicy,
) -> None:
    if request.effect not in _ALLOWED_EFFECTS:
        raise PermissionError(f"codex_run effect is not supported: {request.effect.value}")
    if request.effect not in policy.allowed_effects:
        raise PermissionError(f"codex_run effect is denied by policy: {request.effect.value}")
    if not authority.allows_tool_effect(request.effect):
        raise PermissionError(f"codex_run effect is denied by authority: {request.effect.value}")
    if request.sandbox not in policy.allowed_sandboxes:
        raise PermissionError(f"codex_run sandbox is denied by policy: {request.sandbox}")


def _resolve_workdir(workdir: Path, authority: ParentAuthority) -> Path:
    resolved = workdir.expanduser().resolve(strict=False)
    if not resolved.is_dir():
        raise ValueError(f"codex_run workdir does not exist: {resolved}")
    scopes = tuple(
        Path(scope).expanduser().resolve(strict=False)
        for scope in authority.filesystem_scope
        if scope
    )
    if not scopes:
        raise PermissionError("codex_run requires authority filesystem_scope")
    if not any(_is_relative_to(resolved, scope) for scope in scopes):
        raise PermissionError(f"codex_run workdir is outside authority filesystem_scope: {resolved}")
    return resolved


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
