"""自己開発イベントへ記録する reason の正規化。"""

from __future__ import annotations

from typing import Any


def nonempty_reason(value: Any, *, context: str) -> str:
    """空の例外文字列や空白だけの入力を監査可能な reason にする。"""

    if isinstance(value, BaseException):
        detail = str(value).strip()
        if detail:
            return detail
        return f"{type(value).__name__} ({context})"
    if isinstance(value, str):
        detail = value.strip()
        if detail:
            return detail
    return f"reason unavailable ({context})"


def exception_reason(exc: BaseException, *, context: str) -> str:
    """例外をそのまま reason に使える非空文字列へ変換する。"""

    return nonempty_reason(exc, context=context)


__all__ = ["exception_reason", "nonempty_reason"]
