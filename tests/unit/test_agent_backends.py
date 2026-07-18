"""AgentRuntime CLI バックエンドの決定論テスト。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from vsm.agents.backends.claude_code import ClaudeCodeRuntime
from vsm.agents.backends.codex import CodexRuntime
from vsm.agents.runtime import AgentRequest, AgentRuntimeError


class _Stdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _Process:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0) -> None:
        self.stdin = _Stdin()
        self.returncode = returncode
        self._stdout = stdout.encode()
        self._stderr = stderr.encode()
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


class _Factory:
    def __init__(self, process: _Process) -> None:
        self.process = process
        self.argv: tuple[Any, ...] = ()
        self.kwargs: dict[str, Any] = {}

    async def __call__(self, *argv: Any, **kwargs: Any) -> _Process:
        self.argv = argv
        self.kwargs = kwargs
        return self.process


@pytest.mark.asyncio
async def test_claude_json_usage_and_resume() -> None:
    process = _Process(
        json.dumps(
            {
                "result": "完了",
                "session_id": "session-1",
                "model": "claude-test",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 5,
                },
            }
        )
    )
    factory = _Factory(process)
    runtime = ClaudeCodeRuntime(process_factory=factory)

    result = await runtime.invoke(AgentRequest(prompt="依頼", session_ref="old-session"))

    assert factory.argv[:4] == ("claude", "-p", "--output-format", "json")
    assert factory.argv[-2:] == ("--resume", "old-session")
    assert process.stdin.data == "依頼".encode()
    assert process.stdin.closed
    assert result.text == "完了"
    assert (result.tokens_in, result.tokens_out, result.tokens_cache_read) == (11, 7, 5)
    assert result.session_ref == "session-1"


@pytest.mark.asyncio
async def test_claude_quota_is_result_even_without_json() -> None:
    factory = _Factory(_Process("", "Usage limit reached; resets at 2026-07-12T12:00:00Z", 1))
    result = await ClaudeCodeRuntime(process_factory=factory).invoke(AgentRequest(prompt="依頼"))
    assert result.quota_exhausted is True
    assert result.quota_reset_at is not None


@pytest.mark.asyncio
async def test_codex_jsonl_usage_session_resume_and_stdin_close() -> None:
    stdout = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "thread.started", "thread_id": "thread-2"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "実行済み"},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 21, "cached_input_tokens": 8, "output_tokens": 9},
            },
        )
    )
    process = _Process(stdout)
    factory = _Factory(process)
    runtime = CodexRuntime(
        model="gpt-test",
        reasoning_effort="high",
        process_factory=factory,
    )

    result = await runtime.invoke(AgentRequest(prompt="実装", session_ref="thread-old"))

    assert factory.argv[:4] == ("codex", "exec", "resume", "thread-old")
    assert "--json" in factory.argv
    assert process.stdin.data == "実装".encode()
    assert process.stdin.closed
    assert result.text == "実行済み"
    assert (result.tokens_in, result.tokens_out, result.tokens_cache_read) == (21, 9, 8)
    assert result.session_ref == "thread-2"


@pytest.mark.asyncio
async def test_codex_quota_error_event_is_result() -> None:
    stdout = json.dumps({"type": "error", "message": "rate limit exceeded"})
    result = await CodexRuntime(
        model="gpt-test",
        reasoning_effort="medium",
        process_factory=_Factory(_Process(stdout, returncode=1)),
    ).invoke(AgentRequest(prompt="実装"))
    assert result.quota_exhausted is True


@pytest.mark.asyncio
async def test_cli_invalid_json_is_normalized() -> None:
    runtime = CodexRuntime(
        model="gpt-test",
        reasoning_effort="low",
        process_factory=_Factory(_Process("not-json")),
    )
    with pytest.raises(AgentRuntimeError) as caught:
        await runtime.invoke(AgentRequest(prompt="実装", workdir=Path(".")))
    assert caught.value.code == "invalid_jsonl"


@pytest.mark.asyncio
async def test_codex_cancel_terminates_and_reaps_process() -> None:
    class BlockingProcess(_Process):
        def __init__(self) -> None:
            super().__init__("")
            self.started = asyncio.Event()
            self.terminated = asyncio.Event()

        async def communicate(self) -> tuple[bytes, bytes]:
            self.started.set()
            await self.terminated.wait()
            self.returncode = -1
            return b"", b""

        def kill(self) -> None:
            super().kill()
            self.terminated.set()

    process = BlockingProcess()
    runtime = CodexRuntime(
        model="gpt-test",
        reasoning_effort="high",
        process_factory=_Factory(process),
    )
    task = asyncio.create_task(
        runtime.invoke(AgentRequest(prompt="長時間作業", workdir=Path(".")))
    )
    await asyncio.wait_for(process.started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.done()
    assert process.killed
    assert process.returncode == -1
