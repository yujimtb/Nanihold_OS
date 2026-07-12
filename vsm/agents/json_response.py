"""LLM の JSON 応答を抽出・再質問する共通処理。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from vsm.agents.runtime import AgentResult

__all__ = [
    "JsonObjectExtractionError",
    "JsonResponseParseError",
    "extract_json_object",
    "invoke_with_json_retry",
]


class JsonResponseParseError(ValueError):
    """LLM 応答が要求された JSON 応答契約を満たさない。"""


class JsonObjectExtractionError(JsonResponseParseError):
    """応答から完全な JSON object を抽出できない。"""


def extract_json_object(text: str) -> dict[str, Any]:
    """説明文やコードフェンスを含む応答から最初の JSON object を抽出する。

    ``JSONDecoder.raw_decode`` を各 ``{`` から試すことで、前置き、後置き、
    `````json```` フェンスを許容する。JSON の値そのものは補正せず、壊れた
    応答から内容を捏造することはしない。
    """

    if not isinstance(text, str) or not text.strip():
        raise JsonObjectExtractionError("JSON object を抽出できません: 応答が空です")

    decoder = json.JSONDecoder()
    last_error: json.JSONDecodeError | None = None
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(value, dict):
            return value

    if last_error is None:
        detail = "JSON object の開始位置がありません"
    else:
        detail = f"{last_error.msg} (line {last_error.lineno}, column {last_error.colno})"
    raise JsonObjectExtractionError(f"JSON object を抽出できません: {detail}")


_Parsed = TypeVar("_Parsed")
ResponseInvoker = Callable[[str], Awaitable[AgentResult]]
ResponseObserver = Callable[[int, AgentResult], None]


async def invoke_with_json_retry(
    invoke: ResponseInvoker,
    prompt: str,
    parse: Callable[[str], _Parsed],
    *,
    timeout_seconds: float | None = None,
    response_observer: ResponseObserver | None = None,
) -> tuple[AgentResult, _Parsed]:
    """応答をパースし、契約違反時だけ同じ runtime へ1回再質問する。

    ``invoke`` は呼び出し元が保持する同一 runtime／SubAgent に束縛する。
    再質問では新しい prompt 文字列を渡すため、同一セッションを暗黙に
    継続するかどうかは runtime 側の既存契約に委ねる。実行エラーや timeout
    は再質問せず、そのまま伝播させる。
    """

    async def call(request_prompt: str) -> AgentResult:
        if timeout_seconds is None:
            return await invoke(request_prompt)
        return await asyncio.wait_for(invoke(request_prompt), timeout=timeout_seconds)

    first_result = await call(prompt)
    if response_observer is not None:
        response_observer(1, first_result)
    try:
        return first_result, parse(first_result.text)
    except JsonResponseParseError as first_error:
        retry_prompt = (
            f"{prompt}\n\n"
            "【再質問】前回の応答は出力契約を満たさず、パースに失敗しました。\n"
            f"パースエラー: {first_error}\n"
            "同じ要求に対して、説明文・コードフェンス・前置き・後置きなしで、"
            "期待スキーマに従う JSON object のみを返してください。"
        )
        retry_result = await call(retry_prompt)
        if response_observer is not None:
            response_observer(2, retry_result)
        try:
            return retry_result, parse(retry_result.text)
        except JsonResponseParseError as second_error:
            raise second_error from first_error
