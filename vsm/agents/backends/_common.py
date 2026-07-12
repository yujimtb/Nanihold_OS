"""CLI バックエンド間で共有する小さな補助関数。"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from typing import Any

_QUOTA_MARKERS = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota exceeded",
    "quota_exhausted",
    "too many requests",
    "limit reached",
    "you have no weighted tokens left",
)


def resolve_bin(bin_name: str) -> str:
    """CLI 実行ファイル名を PATH 上の実体パスに解決する。

    Windows では npm の CLI が ``claude.cmd`` のようなシムとして配置され、
    拡張子なしの名前を ``CreateProcess`` に渡すと WinError 2 になる。
    ``shutil.which`` は PATHEXT を考慮して実体を返すため、見つかった場合は
    そのパスを使う。見つからない場合は元の名前を返し、起動時の
    ``process_start_failed`` として通常のエラー経路に乗せる。
    """

    return shutil.which(bin_name) or bin_name


def is_quota_exhausted(text: str, returncode: int | None = None) -> bool:
    """CLI の診断文字列が利用上限を表すかを判定する。"""

    lowered = text.lower()
    return returncode in {29, 75, 429} or any(marker in lowered for marker in _QUOTA_MARKERS)


def parse_quota_reset_at(text: str) -> datetime | None:
    """診断文字列に含まれる ISO 8601 のリセット時刻を抽出する。"""

    match = re.search(
        r"(?:reset(?:s|_at)?|try again)(?:\s+at|\s*[:=])?\s*"
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2}))",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    value = match.group(1).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def as_non_negative_int(value: Any) -> int:
    """トークン値を非負整数へ厳密に正規化する。"""

    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, result)


async def write_and_close_stdin(process: Any, prompt: str) -> None:
    """プロンプトを書き込み、成功・失敗にかかわらず stdin を閉じる。"""

    stream = process.stdin
    if stream is None:
        raise RuntimeError("サブプロセスの stdin が利用できません")
    try:
        stream.write(prompt.encode("utf-8"))
        drain = getattr(stream, "drain", None)
        if drain is not None:
            await drain()
    finally:
        stream.close()
        wait_closed = getattr(stream, "wait_closed", None)
        if wait_closed is not None:
            await wait_closed()
