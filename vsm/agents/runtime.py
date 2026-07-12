"""AI エージェント実行バックエンドの共通契約。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "AgentRequest",
    "AgentResult",
    "AgentRuntimeError",
    "AgentRuntimeProtocol",
]


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """1 回のエージェント呼び出しに渡す値。"""

    prompt: str
    context_view: str | None = None
    session_ref: str | None = None
    workdir: Path | None = None
    model: str | None = None
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("prompt は空にできません")
        if self.model is not None and not self.model.strip():
            raise ValueError("model を指定する場合は空にできません")
        if self.session_ref is not None and not self.session_ref.strip():
            raise ValueError("session_ref を指定する場合は空にできません")
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds は正数でなければなりません")


@dataclass(frozen=True, slots=True)
class AgentResult:
    """エージェント呼び出しの正規化済み結果。"""

    text: str
    tokens_in: int
    tokens_out: int
    tokens_cache_read: int
    latency_ms: int
    model: str
    backend: str
    session_ref: str | None
    quota_exhausted: bool = False
    quota_reset_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("tokens_in", "tokens_out", "tokens_cache_read", "latency_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} は 0 以上でなければなりません")
        if not self.model:
            raise ValueError("model は空にできません")
        if not self.backend:
            raise ValueError("backend は空にできません")


class AgentRuntimeError(Exception):
    """エージェントバックエンドの実行失敗を表す正規化例外。"""

    def __init__(self, *, backend: str, code: str, message: str) -> None:
        self.backend = backend
        self.code = code
        self.message = message
        super().__init__(f"{backend} ({code}): {message}")


@runtime_checkable
class AgentRuntimeProtocol(Protocol):
    """差し替え可能な AI エージェント実行インターフェース。"""

    backend_name: str
    timeout_seconds: float

    async def invoke(self, request: AgentRequest) -> AgentResult:
        """リクエストを実行して正規化済み結果を返す。"""
        ...
