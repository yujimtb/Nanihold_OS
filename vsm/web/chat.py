"""Web UI から AgentRuntime を継続利用する対話セッション。"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from vsm.agents.backends import ClaudeCodeRuntime, CodexRuntime
from vsm.agents.runtime import AgentRequest, AgentResult, AgentRuntimeProtocol
from vsm.config import load_config
from vsm.ids import generate_uuid

ChatBackend = Literal["claude-code", "codex"]
DEFAULT_CHAT_TIMEOUT_SECONDS = 300.0
DEFAULT_WORKDIR = Path(__file__).resolve().parents[2]


class ChatBusyError(RuntimeError):
    """同一チャットで別のCLI呼び出しが進行中。"""


class ChatTimeoutError(TimeoutError):
    """チャット応答が設定された制限時間を超過した。"""


RuntimeFactory = Callable[[ChatBackend, str | None], AgentRuntimeProtocol]


@dataclass
class ChatMessage:
    message_id: str
    role: Literal["user", "assistant"]
    text: str
    tokens: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache_read: int = 0
    latency_ms: int = 0
    created_at: str = ""
    session_ref: str | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "text": self.text,
            "tokens": self.tokens,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_cache_read": self.tokens_cache_read,
            "latency_ms": self.latency_ms,
            "created_at": self.created_at,
        }


@dataclass
class ChatSession:
    chat_id: str
    backend: ChatBackend
    model: str | None
    workdir: Path
    runtime: AgentRuntimeProtocol
    session_ref: str | None = None
    messages: list[ChatMessage] = field(default_factory=list)
    total_tokens: int = 0
    busy: bool = False
    state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "backend": self.backend,
            "model": self.model,
            "workdir": str(self.workdir),
            "session_ref": self.session_ref,
            "messages": [message.public_dict() for message in self.messages],
            "total_tokens": self.total_tokens,
        }


class ChatManager:
    """チャットセッションのライフサイクルとJSONL永続化を管理する。"""

    def __init__(
        self,
        root: Path,
        *,
        runtime_factory: RuntimeFactory | None = None,
        timeout_seconds: float = DEFAULT_CHAT_TIMEOUT_SECONDS,
        default_workdir: Path = DEFAULT_WORKDIR,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("チャットの timeout_seconds は正数でなければなりません")
        if not default_workdir.is_dir():
            raise ValueError(f"作業ディレクトリが存在しません: {default_workdir}")
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = float(timeout_seconds)
        self.default_workdir = default_workdir.resolve()
        self._runtime_factory = runtime_factory or self._build_runtime
        self._sessions: dict[str, ChatSession] = {}
        self._load_sessions()

    def create_session(
        self,
        *,
        backend: ChatBackend,
        model: str | None = None,
        workdir: str | None = None,
    ) -> dict[str, Any]:
        if backend not in {"claude-code", "codex"}:
            raise ValueError("backend は claude-code または codex でなければなりません")
        if model is not None:
            model = model.strip()
            if not model:
                raise ValueError("model を指定する場合は空にできません")
        resolved_workdir = self._resolve_workdir(workdir)
        chat_id = f"chat-{generate_uuid()}"
        runtime = self._runtime_factory(backend, model)
        session = ChatSession(
            chat_id=chat_id,
            backend=backend,
            model=model,
            workdir=resolved_workdir,
            runtime=runtime,
        )
        self._sessions[chat_id] = session
        self._append(
            chat_id,
            {
                "event": "session_created",
                "chat_id": chat_id,
                "backend": backend,
                "model": model,
                "workdir": str(resolved_workdir),
            },
        )
        return session.public_dict()

    async def send_message(self, chat_id: str, text: str) -> dict[str, Any]:
        session = self.get_session(chat_id)
        cleaned = text.strip()
        if not cleaned:
            raise ValueError("メッセージを入力してください")

        with session.state_lock:
            if session.busy:
                raise ChatBusyError("この対話では別のメッセージを処理中です")
            session.busy = True

        user_message = ChatMessage(
            message_id=generate_uuid(),
            role="user",
            text=cleaned,
            created_at=utc_now(),
        )
        session.messages.append(user_message)
        self._append(
            chat_id,
            {
                "event": "message",
                "message": user_message.public_dict(),
            },
        )
        try:
            request = AgentRequest(
                prompt=cleaned,
                session_ref=session.session_ref,
                workdir=session.workdir,
                model=session.model,
                timeout_seconds=self.timeout_seconds,
            )
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    session.runtime.invoke(request),
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise ChatTimeoutError(
                    f"対話応答が {self.timeout_seconds:g} 秒でタイムアウトしました"
                ) from exc
            measured_latency_ms = max(0, int((time.monotonic() - started) * 1000))
            response = self._make_assistant_message(result, measured_latency_ms)
            session.messages.append(response)
            session.session_ref = result.session_ref
            session.total_tokens += response.tokens
            self._append(
                chat_id,
                {
                    "event": "message",
                    "message": response.public_dict(),
                    "session_ref": session.session_ref,
                },
            )
            return {
                "chat_id": chat_id,
                "text": response.text,
                "tokens": response.tokens,
                "latency": response.latency_ms,
                "latency_ms": response.latency_ms,
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "tokens_cache_read": response.tokens_cache_read,
                "session_ref": session.session_ref,
                "message": response.public_dict(),
            }
        finally:
            with session.state_lock:
                session.busy = False

    def get_session(self, chat_id: str) -> ChatSession:
        session = self._sessions.get(chat_id)
        if session is None:
            raise KeyError(chat_id)
        return session

    def history(self, chat_id: str) -> dict[str, Any]:
        return self.get_session(chat_id).public_dict()

    def _make_assistant_message(
        self, result: AgentResult, measured_latency_ms: int
    ) -> ChatMessage:
        tokens = result.tokens_in + result.tokens_out
        return ChatMessage(
            message_id=generate_uuid(),
            role="assistant",
            text=result.text,
            tokens=tokens,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            tokens_cache_read=result.tokens_cache_read,
            latency_ms=result.latency_ms or measured_latency_ms,
            created_at=utc_now(),
            session_ref=result.session_ref,
        )

    def _resolve_workdir(self, workdir: str | None) -> Path:
        if workdir is None:
            return self.default_workdir
        cleaned = workdir.strip()
        if not cleaned:
            raise ValueError("workdir を指定する場合は空にできません")
        resolved = Path(cleaned).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"作業ディレクトリが存在しません: {resolved}")
        return resolved

    def _append(self, chat_id: str, payload: dict[str, Any]) -> None:
        path = self.root / f"{chat_id}.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _load_sessions(self) -> None:
        for path in sorted(self.root.glob("chat-*.jsonl")):
            session, events = self._read_session(path)
            if session.chat_id in self._sessions:
                raise RuntimeError(f"チャットIDが重複しています: {session.chat_id}")
            self._sessions[session.chat_id] = session
            for event in events:
                self._restore_event(session, event)

    def _read_session(self, path: Path) -> tuple[ChatSession, list[dict[str, Any]]]:
        events: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"チャット履歴を解析できません: {path}:{line_number}") from exc
                if not isinstance(event, dict):
                    raise RuntimeError(f"チャット履歴のイベントがオブジェクトではありません: {path}:{line_number}")
                events.append(event)
        if not events or events[0].get("event") != "session_created":
            raise RuntimeError(f"チャット履歴の先頭が session_created ではありません: {path}")
        created = events[0]
        chat_id = created.get("chat_id")
        backend = created.get("backend")
        model = created.get("model")
        workdir = created.get("workdir")
        if not isinstance(chat_id, str) or not isinstance(backend, str) or backend not in {"claude-code", "codex"}:
            raise RuntimeError(f"チャット履歴のセッション情報が不正です: {path}")
        if model is not None and not isinstance(model, str):
            raise RuntimeError(f"チャット履歴のmodelが不正です: {path}")
        if not isinstance(workdir, str):
            raise RuntimeError(f"チャット履歴のworkdirが不正です: {path}")
        # 履歴は別ホスト(例: Windowsホストとコンテナ)間で共有され得るため、
        # 復元時の workdir 不在は起動失敗にせず default_workdir へ退避する。
        # 厳格な検証は新規セッション作成・メッセージ送信時に行う。
        try:
            resolved_workdir = self._resolve_workdir(workdir)
        except ValueError:
            resolved_workdir = self.default_workdir
        runtime = self._runtime_factory(backend, model)
        return (
            ChatSession(
                chat_id=chat_id,
                backend=backend,
                model=model,
                workdir=resolved_workdir,
                runtime=runtime,
            ),
            events[1:],
        )

    def _restore_event(self, session: ChatSession, event: dict[str, Any]) -> None:
        if event.get("event") != "message":
            raise RuntimeError(f"未知のチャットイベントです: {event.get('event')!r}")
        raw_message = event.get("message")
        if not isinstance(raw_message, dict):
            raise RuntimeError("チャット message イベントの形式が不正です")
        role = raw_message.get("role")
        if role not in {"user", "assistant"}:
            raise RuntimeError("チャットメッセージのroleが不正です")
        message = ChatMessage(
            message_id=self._required_string(raw_message, "message_id"),
            role=role,
            text=self._required_string(raw_message, "text"),
            tokens=int(raw_message.get("tokens", 0)),
            tokens_in=int(raw_message.get("tokens_in", 0)),
            tokens_out=int(raw_message.get("tokens_out", 0)),
            tokens_cache_read=int(raw_message.get("tokens_cache_read", 0)),
            latency_ms=int(raw_message.get("latency_ms", 0)),
            created_at=self._required_string(raw_message, "created_at"),
            session_ref=event.get("session_ref") if role == "assistant" else None,
        )
        session.messages.append(message)
        if role == "assistant":
            session.total_tokens += message.tokens
            session.session_ref = event.get("session_ref")

    @staticmethod
    def _required_string(payload: dict[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str):
            raise RuntimeError(f"チャットメッセージの{key}が不正です")
        return value

    @staticmethod
    def _build_runtime(backend: ChatBackend, model: str | None) -> AgentRuntimeProtocol:
        _, run_config = load_config(None)
        backend_config = run_config.agents.backends[backend]
        if backend == "claude-code":
            if backend_config.bin is None:
                raise RuntimeError("claude-code の実行ファイルが設定されていません")
            return ClaudeCodeRuntime(
                claude_bin=backend_config.bin,
                model=model if model is not None else backend_config.model,
                timeout_seconds=DEFAULT_CHAT_TIMEOUT_SECONDS,
            )
        if backend_config.bin is None or backend_config.reasoning_effort is None:
            raise RuntimeError("codex の実行設定が不足しています")
        return CodexRuntime(
            codex_bin=backend_config.bin,
            model=model if model is not None else backend_config.model,
            reasoning_effort=backend_config.reasoning_effort,
            timeout_seconds=DEFAULT_CHAT_TIMEOUT_SECONDS,
        )


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
