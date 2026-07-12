"""Codex CLI を利用する AgentRuntime。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from vsm.agents.backends._common import (
    as_non_negative_int,
    is_quota_exhausted,
    parse_quota_reset_at,
    resolve_bin,
    write_and_close_stdin,
)
from vsm.agents.runtime import AgentRequest, AgentResult, AgentRuntimeError


class CodexRuntime:
    """Codex の JSONL イベントを AgentResult に集約する。"""

    backend_name = "codex"

    def __init__(
        self,
        *,
        codex_bin: str = "codex",
        model: str,
        reasoning_effort: str,
        timeout_seconds: float = 1800.0,
        process_factory: Callable[..., Any] = asyncio.create_subprocess_exec,
    ) -> None:
        if not codex_bin.strip():
            raise ValueError("codex_bin は空にできません")
        if not model.strip():
            raise ValueError("Codex の model は空にできません")
        if not reasoning_effort.strip():
            raise ValueError("Codex の reasoning_effort は空にできません")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds は正数でなければなりません")
        self.codex_bin = codex_bin
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self._process_factory = process_factory

    async def invoke(self, request: AgentRequest) -> AgentResult:
        prompt = request.prompt
        if request.context_view:
            prompt = f"{request.context_view}\n\n{prompt}"
        model = request.model or self.model
        if request.session_ref:
            argv = [resolve_bin(self.codex_bin), "exec", "resume", request.session_ref]
            argv.extend(["--json", "-m", model])
        else:
            argv = [resolve_bin(self.codex_bin), "exec", "--json", "-m", model]
        argv.extend(["-c", f"model_reasoning_effort={self.reasoning_effort}"])

        started = time.monotonic()
        try:
            process = await self._process_factory(
                *argv,
                cwd=request.workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await write_and_close_stdin(process, prompt)
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=request.timeout_seconds or self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            if "process" in locals():
                process.kill()
                await process.communicate()
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
        quota = is_quota_exhausted(combined, returncode)
        try:
            events = _parse_jsonl(stdout)
        except AgentRuntimeError:
            if not quota:
                raise
            events = []
        if returncode != 0 and not quota:
            raise AgentRuntimeError(
                backend=self.backend_name,
                code=f"exit_{returncode}",
                message=stderr.strip() or stdout.strip() or "Codex が異常終了しました",
            )
        if not events and not quota:
            raise AgentRuntimeError(
                backend=self.backend_name,
                code="empty_output",
                message="Codex が JSONL イベントを返しませんでした",
            )
        event_error = _extract_error(events)
        if event_error is not None and not quota:
            raise AgentRuntimeError(
                backend=self.backend_name,
                code="cli_error",
                message=event_error,
            )

        tokens_in, tokens_out, cache_read = _extract_usage(events)
        text = _extract_text(events) or (stderr.strip() if quota else "")
        return AgentResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cache_read=cache_read,
            latency_ms=latency_ms,
            model=model,
            backend=self.backend_name,
            session_ref=_extract_session_ref(events),
            quota_exhausted=quota,
            quota_reset_at=parse_quota_reset_at(combined),
        )


def _parse_jsonl(stdout: str) -> list[Mapping[str, Any]]:
    events: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AgentRuntimeError(
                backend="codex",
                code="invalid_jsonl",
                message=f"Codex JSONL の {line_number} 行目を解析できません: {exc}",
            ) from exc
        if not isinstance(event, Mapping):
            raise AgentRuntimeError(
                backend="codex",
                code="invalid_jsonl",
                message=f"Codex JSONL の {line_number} 行目はオブジェクトではありません",
            )
        events.append(event)
    return events


def _extract_usage(events: list[Mapping[str, Any]]) -> tuple[int, int, int]:
    latest: Mapping[str, Any] = {}
    for event in events:
        candidates = [event.get("usage")]
        info = event.get("info")
        if isinstance(info, Mapping):
            candidates.extend([info.get("usage"), info.get("total_token_usage")])
        for candidate in candidates:
            if isinstance(candidate, Mapping):
                latest = candidate
    return (
        as_non_negative_int(latest.get("input_tokens") or latest.get("prompt_tokens")),
        as_non_negative_int(latest.get("output_tokens") or latest.get("completion_tokens")),
        as_non_negative_int(
            latest.get("cached_input_tokens") or latest.get("cache_read_input_tokens")
        ),
    )


def _extract_session_ref(events: list[Mapping[str, Any]]) -> str | None:
    for event in events:
        for key in ("thread_id", "session_id", "conversation_id"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        thread = event.get("thread")
        if isinstance(thread, Mapping):
            value = thread.get("id")
            if isinstance(value, str) and value:
                return value
    return None


def _extract_text(events: list[Mapping[str, Any]]) -> str:
    messages: list[str] = []
    for event in events:
        item = event.get("item")
        if isinstance(item, Mapping) and item.get("type") in {"agent_message", "message"}:
            value = item.get("text") or item.get("content")
            if isinstance(value, str):
                messages.append(value)
        if event.get("type") in {"agent_message", "message"}:
            value = event.get("text") or event.get("content")
            if isinstance(value, str):
                messages.append(value)
    return messages[-1] if messages else ""


def _extract_error(events: list[Mapping[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") not in {"error", "turn.failed", "item.failed"}:
            continue
        value = event.get("message") or event.get("error")
        if isinstance(value, str) and value:
            return value
        if isinstance(value, Mapping):
            message = value.get("message")
            if isinstance(message, str) and message:
                return message
        return "Codex がエラーイベントを返しました"
    return None
