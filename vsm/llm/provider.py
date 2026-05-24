"""LiteLLM-backed implementation of :class:`LLMProviderProtocol` (Task 9.1).

design.md `## Components and Interfaces` §4 (LLM_Provider_Abstraction) の
擬似コードに対応する本番実装。``litellm.acompletion`` を呼び出し、
プロバイダー側エラーを :class:`vsm.errors.LLMProviderError` に変換する。

Defense-in-depth timeout strategy
---------------------------------
本モジュールは ``litellm.acompletion(timeout=60)`` で **SDK レイヤの
タイムアウト** を強制する。一方、呼び出し側 (``SubAgent.respond`` /
Task 10.1) は ``asyncio.wait_for(provider.invoke(...), 60)`` で
**asyncio レイヤのタイムアウト** をかける。これにより SDK 内のスレッド
プールが詰まった場合でも 60 秒以内に確実に ``asyncio.TimeoutError``
として伝達でき、REQ 3.4 / 3.5 の SLA を二重防衛で保証する。

Requirements traced
-------------------
- REQ 3.1: 少なくとも 1 つの Provider (LiteLLM 経由) をサポートする。
- REQ 3.4: ``timeout=60`` を SDK 引数に渡し SDK 側で打ち切る。
- REQ 3.6: ``litellm.exceptions.APIError`` を ``LLMProviderError`` に
  変換し、code / message を保持して呼び出し側に伝える。
- REQ 3.7: 既定モデルは ``LLMConfig.resolve_model()`` から解決され、
  System / Sub_Agent コードに触れずに provider を差し替えられる。
"""

from __future__ import annotations

import time
from typing import Any

from vsm.config import LLMConfig
from vsm.errors import LLMProviderError
from vsm.llm.types import LLMResponse


__all__ = ["LLMProvider"]


# ``litellm`` は依存ツリーが大きく、テスト環境やオフラインビルドでは
# 未インストールの場合がある。本モジュールが import できるだけで Sub_Agent
# 配線などの単体テストが通るよう、import は ``invoke`` 内に遅延させる。


class LLMProvider:
    """LiteLLM-backed implementation of :class:`LLMProviderProtocol`.

    本クラスは VSM_Platform の本番経路で唯一 ``litellm`` を直接
    呼び出す場所であり、テストでは :class:`vsm.llm.fake.FakeLLMProvider`
    (Task 9.2) で差し替えられる (REQ 3.1, 3.7)。

    Parameters
    ----------
    config : LLMConfig
        ``LITELLM_PROVIDER`` 環境変数 / ``vsm.toml`` から解決される
        既定モデル設定 (REQ 3.7)。

    Notes
    -----
    呼び出し側は :func:`asyncio.wait_for` で ``invoke`` をラップし、
    SDK タイムアウト (60 s) と asyncio タイムアウト (60 s) の二重防衛を
    取る前提である (REQ 3.4 / 3.5)。
    """

    #: 単一呼び出しのタイムアウト秒数 (REQ 3.4)。
    TIMEOUT_SECONDS: int = 60

    def __init__(self, config: LLMConfig) -> None:
        self._config: LLMConfig = config
        # ``resolve_model`` は env > file > error の優先順で動作する
        # (REQ 3.7)。env / file いずれも設定されていない場合は構築時に
        # ``ConfigError`` が伝播し、Run start 前に検出される。
        self._default_model: str = config.resolve_model()

    @property
    def default_model(self) -> str:
        """解決済みの既定モデル文字列を返す (Event_Log / 診断用)。"""
        return self._default_model

    async def invoke(self, prompt: str, model: str | None = None) -> LLMResponse:
        """``prompt`` を LiteLLM 経由で送信し :class:`LLMResponse` を返す。

        Parameters
        ----------
        prompt : str
            ユーザープロンプト本文。
        model : str | None
            ``None`` の場合は ``self._default_model`` (LLMConfig 解決値,
            REQ 3.7) が使われる。

        Returns
        -------
        LLMResponse
            ``text`` / ``tokens_in`` / ``tokens_out`` / ``latency_ms`` /
            ``model`` を含む値オブジェクト。``latency_ms`` は LiteLLM が
            ``_response_ms`` を提供しない場合に備えて
            ``time.monotonic`` で実測した値で補完する。

        Raises
        ------
        LLMProviderError
            プロバイダー側エラー (HTTP 4xx/5xx 等) が返ったとき (REQ 3.6)。
            また、``litellm`` が import できない場合にも本例外を起点
            (``code="import_error"``) で raise する。
        """
        # 遅延 import: ``litellm`` は重く、テスト環境では未導入のことも
        # ある。``ImportError`` も :class:`LLMProviderError` に正規化し、
        # 呼び出し側 (Sub_Agent.respond) からは Provider レイヤの統一
        # 例外として観測できるようにする (REQ 3.6)。
        try:
            import litellm  # type: ignore[import-not-found]
            from litellm.exceptions import APIError  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - 依存欠如時のみ通る
            raise LLMProviderError(
                code="import_error",
                message=f"litellm is not installed: {exc}",
            ) from exc

        chosen_model = model or self._default_model
        start = time.monotonic()
        try:
            resp: Any = await litellm.acompletion(
                model=chosen_model,
                messages=[{"role": "user", "content": prompt}],
                timeout=self.TIMEOUT_SECONDS,
            )
        except APIError as exc:
            # ``status_code`` は SDK / プロバイダーによって有無が異なる
            # ため、無い場合は ``"unknown"`` で正規化する。
            code = getattr(exc, "status_code", None)
            if code is None:
                code = "unknown"
            raise LLMProviderError(code=code, message=str(exc)) from exc

        elapsed_ms = int((time.monotonic() - start) * 1000)

        # LiteLLM は OpenAI 互換のレスポンス構造を返すため、属性アクセス
        # と ``["..."]`` 双方に耐えるよう防御的に取り出す。
        choice = resp.choices[0]
        if hasattr(choice, "message"):
            content = choice.message.content
        else:  # pragma: no cover - 防御的フォールバック
            content = choice["message"]["content"]
        text = content or ""

        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0

        # LiteLLM が ``_response_ms`` を提供している場合はそちらを優先し、
        # 無ければ実測値 (``elapsed_ms``) を使う。Provider 自己申告は
        # ネットワーク遅延を含む正確な観測値となる。
        provider_latency = getattr(resp, "_response_ms", None)
        if isinstance(provider_latency, (int, float)) and provider_latency >= 0:
            latency_ms = int(provider_latency)
        else:
            latency_ms = elapsed_ms

        return LLMResponse(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            model=chosen_model,
        )
