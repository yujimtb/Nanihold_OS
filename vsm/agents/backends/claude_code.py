"""Claude Code CLI を利用する AgentRuntime。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from vsm.agents.backends._common import (
    as_non_negative_int,
    detect_quota_kind,
    is_quota_exhausted,
    parse_quota_reset_at,
    process_group_kwargs,
    resolve_bin,
    terminate_process_group,
    write_and_close_stdin,
)
from vsm.agents.runtime import AgentRequest, AgentResult, AgentRuntimeError


class ClaudeCodeRuntime:
    """``claude -p --output-format json`` の実行結果を正規化する。"""

    backend_name = "claude-code"
    quota_pool = "claude-subscription"

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        model: str = "",
        timeout_seconds: float = 1800.0,
        process_factory: Callable[..., Any] = asyncio.create_subprocess_exec,
    ) -> None:
        if not claude_bin.strip():
            raise ValueError("claude_bin は空にできません")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds は正数でなければなりません")
        self.claude_bin = claude_bin
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._process_factory = process_factory

    async def invoke(self, request: AgentRequest) -> AgentResult:
        prompt = request.prompt
        if request.context_view:
            prompt = f"{request.context_view}\n\n{prompt}"
        model = request.model if request.model is not None else self.model
        argv = [resolve_bin(self.claude_bin), "-p", "--output-format", "json"]
        if model:
            argv.extend(["--model", model])
        if request.session_ref:
            argv.extend(["--resume", request.session_ref])

        started = time.monotonic()
        process = None
        try:
            process = await self._process_factory(
                *argv,
                cwd=request.workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_group_kwargs(),
            )
            await write_and_close_stdin(process, prompt)
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=request.timeout_seconds or self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            if process is not None:
                await terminate_process_group(process)
            raise
        except asyncio.CancelledError:
            if process is not None:
                await terminate_process_group(process)
            raise
        except OSError as exc:
            raise AgentRuntimeError(
                backend=self.backend_name,
                code="process_start_failed",
                message=str(exc),
            ) from exc
        except Exception as exc:
            if isinstance(exc, AgentRuntimeError):
                raise
            if process is not None:
                await terminate_process_group(process)
            raise AgentRuntimeError(
                backend=self.backend_name,
                code="process_io_failed",
                message=str(exc),
            ) from exc

        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        combined = f"{stdout}\n{stderr}"
        returncode = process.returncode if process.returncode is not None else 1

        payload: Mapping[str, Any] | None = None
        if stdout.strip():
            try:
                decoded = json.loads(stdout)
            except json.JSONDecodeError as exc:
                if not is_quota_exhausted(combined, returncode):
                    raise AgentRuntimeError(
                        backend=self.backend_name,
                        code="invalid_json",
                        message=f"Claude Code の JSON 出力を解析できません: {exc}",
                    ) from exc
            else:
                if not isinstance(decoded, Mapping):
                    raise AgentRuntimeError(
                        backend=self.backend_name,
                        code="invalid_json",
                        message="Claude Code の JSON 出力はオブジェクトではありません",
                    )
                payload = decoded

        quota = is_quota_exhausted(combined, returncode)
        if returncode != 0 and not quota:
            raise AgentRuntimeError(
                backend=self.backend_name,
                code=f"exit_{returncode}",
                message=stderr.strip() or stdout.strip() or "Claude Code が異常終了しました",
            )
        if payload is None and not quota:
            raise AgentRuntimeError(
                backend=self.backend_name,
                code="empty_output",
                message="Claude Code が JSON を返しませんでした",
            )
        if payload is not None and _payload_is_error(payload) and not quota:
            raise AgentRuntimeError(
                backend=self.backend_name,
                code="cli_error",
                message=_extract_text(payload) or stderr.strip() or "Claude Code がエラーを返しました",
            )

        usage = payload.get("usage", {}) if payload is not None else {}
        if not isinstance(usage, Mapping):
            usage = {}
        text = _extract_text(payload) if payload is not None else stderr.strip()
        session_ref = _optional_string(payload.get("session_id")) if payload else None
        actual_model = (
            _optional_string(payload.get("model")) if payload else None
        ) or model or "claude-subscription-default"
        return AgentResult(
            text=text,
            tokens_in=as_non_negative_int(usage.get("input_tokens")),
            tokens_out=as_non_negative_int(usage.get("output_tokens")),
            tokens_cache_read=as_non_negative_int(usage.get("cache_read_input_tokens")),
            latency_ms=latency_ms,
            model=actual_model,
            backend=self.backend_name,
            session_ref=session_ref,
            quota_exhausted=quota,
            quota_reset_at=parse_quota_reset_at(combined),
            quota_kind=detect_quota_kind(combined) if quota else "unknown",
        )


def _extract_text(payload: Mapping[str, Any]) -> str:
    for key in ("result", "text", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping) and isinstance(value.get("content"), str):
            return value["content"]
    return ""


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _payload_is_error(payload: Mapping[str, Any]) -> bool:
    return payload.get("is_error") is True or payload.get("subtype") in {
        "error",
        "error_during_execution",
    }
