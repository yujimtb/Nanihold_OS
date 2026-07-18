"""CLI バックエンド間で共有する小さな補助関数。"""

from __future__ import annotations

import re
import asyncio
import os
import signal
import shutil
import subprocess
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

_WEEKLY_MARKERS = (
    "weekly",
    "week",
    "7-day",
    "7 day",
    "seven-day",
    "seven day",
)

_FIVE_HOUR_MARKERS = (
    "5-hour",
    "5 hour",
    "five-hour",
    "five hour",
    "5h limit",
    "five-hour limit",
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


def process_group_kwargs() -> dict[str, int | bool]:
    """CLI を独立した process group/session で起動するための引数。"""

    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        }
    return {"start_new_session": True}


async def terminate_process_group(process: Any) -> None:
    """子孫を含む CLI process group を終了し、親の終了を確認する。

    ``asyncio.subprocess.Process.kill`` は POSIX では親だけを終了させるため、
    timeout/cancel 経路では必ずこの関数を通す。テスト用 process が PID を
    持たない場合だけ、その process 自身の ``kill`` を使う。
    """

    pid = getattr(process, "pid", None)
    if pid is None:
        kill = getattr(process, "kill", None)
        if kill is None:
            raise RuntimeError("終了対象 process に pid/kill がありません")
        kill()
        communicate = getattr(process, "communicate", None)
        if communicate is not None:
            await communicate()
        return

    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await killer.communicate()
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    await process.communicate()
    if getattr(process, "returncode", None) is not None:
        return

    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await killer.communicate()
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.communicate()


def is_quota_exhausted(text: str, returncode: int | None = None) -> bool:
    """CLI の診断文字列が利用上限を表すかを判定する。"""

    lowered = text.lower()
    return returncode in {29, 75, 429} or any(marker in lowered for marker in _QUOTA_MARKERS) or (
        any(marker in lowered for marker in _WEEKLY_MARKERS + _FIVE_HOUR_MARKERS)
        and ("limit" in lowered or "quota" in lowered)
    )


def detect_quota_kind(text: str) -> str:
    """CLI 診断文言から quota の期間種別を判別する。

    期間が明示されていない診断は、誤って長い weekly 待機へ送らないため
    ``unknown`` とする。判定は quota の診断文言を受けた呼び出し側で行う。
    """

    lowered = text.lower()
    weekly = any(marker in lowered for marker in _WEEKLY_MARKERS) or bool(
        re.search(r'["\']?window_minutes["\']?\s*[:=]\s*10080\b', lowered)
    )
    five_hour = any(marker in lowered for marker in _FIVE_HOUR_MARKERS) or bool(
        re.search(r'["\']?window_minutes["\']?\s*[:=]\s*300\b', lowered)
    )
    if weekly == five_hour:
        return "unknown"
    if weekly:
        return "weekly"
    if five_hour:
        return "five_hour"
    return "unknown"


def parse_quota_reset_at(text: str) -> datetime | None:
    """診断文字列に含まれる ISO 8601 / Unix epoch の reset 時刻を抽出する。"""

    match = re.search(
        r'''(?:["']?resets?(?:_at)?["']?|try again)'''
        r'''(?:\s+at|\s*[:=])?\s*["']?'''
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2}))",
        text,
        flags=re.IGNORECASE,
    )
    if match is not None:
        value = match.group(1).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc)

    epoch_match = re.search(
        r'''["']?resets?_at["']?\s*[:=]\s*["']?(\d{10}(?:\.\d+)?)''',
        text,
        flags=re.IGNORECASE,
    )
    if epoch_match is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_match.group(1)), tz=timezone.utc)
    except (OverflowError, ValueError):
        return None


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
