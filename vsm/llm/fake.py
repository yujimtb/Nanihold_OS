"""Test double for :class:`LLMProviderProtocol` (Task 9.2).

design.md `## Testing Strategy` §LLM モック戦略 に対応する。本モジュールは
全 PBT (P1〜P17) および統合テストで実 LLM 呼び出しを置き換えるための
:class:`FakeLLMProvider` を提供する。``litellm`` を一切 import しないため、
依存ツリーが軽く、決定論的なテストを書ける。

Controllable axes
-----------------
- **応答テキスト** (``response``): 固定文字列 / callable で動的生成。
  callable は ``(prompt, model) -> LLMResponse | str`` のシグネチャで、
  pattern マッチ的な振る舞いも記述できる。
- **レイテンシ** (``latency``): 秒単位の float。``asyncio.sleep`` で消費し、
  ``FakeClock`` と組み合わせれば仮想時間でも実時間でも制御可能。
- **エラー注入** (``error``): :class:`vsm.errors.LLMProviderError` を保持
  しておくと ``invoke`` で raise される。タイムアウトを再現したい場合は
  ``latency`` を 60 秒以上に設定し、呼び出し側で
  ``asyncio.wait_for(invoke(), 60)`` を使うか、ヘルパ
  :func:`make_timeout_provider` を利用する。

Tracking
--------
``invocations`` 属性に ``{"prompt": ..., "model": ...}`` の辞書を append
順で記録する。テストはこの履歴を introspect することで、どの prompt が
どの model 名で呼ばれたかをアサートできる。

Requirements traced
-------------------
- REQ 3.4: ``latency`` を 60 秒以上に設定することで「タイムアウト」
  シナリオを再現できる (実際のキャンセルは caller の
  ``asyncio.wait_for`` が担当する)。
- REQ 3.5: ``make_timeout_provider`` ヘルパが REQ 3.4/3.5 の SLA テスト
  (1 秒以内のエラー伝達) を検証するための入力を提供する。
- REQ 3.6: ``error`` フィールドに :class:`LLMProviderError` を渡すと、
  実プロバイダーのエラーを発火させずに変換ロジックを単独テストできる。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Union

from vsm.errors import LLMProviderError
from vsm.llm.types import LLMResponse


__all__ = [
    "ResponseSpec",
    "FakeLLMProvider",
    "make_timeout_provider",
    "make_error_provider",
]


#: ``response`` フィールドが受け付ける型。
#:
#: - ``str``: 固定の応答テキスト。
#: - ``Callable[[prompt, model], LLMResponse | str]``: 動的生成。
#:   :class:`LLMResponse` を直接返した場合はそのまま採用される
#:   (tokens / latency_ms / model を上書きしたいとき用)。
ResponseSpec = Union[str, Callable[[str, Union[str, None]], "LLMResponse | str"]]


@dataclass
class FakeLLMProvider:
    """:class:`LLMProviderProtocol` を満たすテスト用フェイク実装。

    全 PBT および統合テストで使用するモック (design.md §Testing Strategy
    §LLM モック戦略)。応答 / レイテンシ / エラーをコンストラクタ引数で
    制御し、副作用は ``invocations`` 履歴に閉じ込める。

    Parameters
    ----------
    response : ResponseSpec
        固定文字列または ``(prompt, model) -> LLMResponse | str`` の
        callable。デフォルトは ``"ok"``。
    latency : float
        ``invoke`` 呼び出しごとに ``asyncio.sleep(latency)`` で消費する
        秒数。0 (既定) ならスリープしない。テストで 60 秒タイムアウトを
        誘発したい場合は ``latency=70.0`` のように設定する (REQ 3.4)。
    error : LLMProviderError | None
        非 ``None`` のときは ``invoke`` が常に本例外を raise する
        (REQ 3.6)。
    model : str
        ``invoke`` の ``model`` 引数が ``None`` のときに採用される既定
        モデル名。既定値 ``"fake/test-model"`` は実プロバイダーと
        衝突しない命名にしてある。
    tokens_in, tokens_out : int
        :class:`LLMResponse` に詰める固定トークン数。デフォルトは 1。
    invocations : list[dict[str, Any]]
        呼び出し履歴。各要素は ``{"prompt": str, "model": str}``。
        テスト introspection 用で、外部から書き換えてはならない。
    """

    response: ResponseSpec = "ok"
    latency: float = 0.0
    error: LLMProviderError | None = None
    model: str = "fake/test-model"
    tokens_in: int = 1
    tokens_out: int = 1

    # 履歴 (テスト用 introspection)。``field(default_factory=list)`` で
    # インスタンス毎に独立したリストを確保する。
    invocations: list[dict[str, Any]] = field(default_factory=list)

    async def invoke(self, prompt: str, model: str | None = None) -> LLMResponse:
        """``LLMProviderProtocol.invoke`` の互換実装。

        実行順序は (1) 履歴記録 → (2) ``latency`` 消費 → (3) エラー注入 →
        (4) ``response`` の解決 → (5) :class:`LLMResponse` 構築。
        ``latency`` を消費した後にエラーを raise するため、タイムアウト
        テスト (caller 側の ``asyncio.wait_for(60)``) でも ``error``
        フィールドが効くまで待たずに済む — 通常 ``error`` と ``latency``
        を同時に高い値で組み合わせるユースケースは無い。
        """
        chosen_model = model if model is not None else self.model
        # 履歴は呼び出し開始時点で記録する。タイムアウトやエラー時にも
        # 「呼び出されたこと」を観測できるよう、sleep / raise の前に行う。
        self.invocations.append({"prompt": prompt, "model": chosen_model})

        # レイテンシ制御。``FakeClock`` ベースのテストでは pytest-asyncio
        # の event loop が ``asyncio.sleep`` をフックして仮想時間を進める
        # 構成も可能。実時間で動かす場合でも、テストは小さい値 (50 ms)
        # を使う前提なので問題ない。
        if self.latency > 0:
            await asyncio.sleep(self.latency)

        # エラー注入 (REQ 3.6)。caller の `SubAgent.respond` は本例外を
        # 捕捉し ``llm_error`` event を 1 秒以内に append する責務を持つ。
        if self.error is not None:
            raise self.error

        # ``response`` の解決。callable は :class:`LLMResponse` を直接
        # 返してもよい (model 名や tokens を上書きしたいとき用)。
        if callable(self.response):
            result = self.response(prompt, chosen_model)
            if isinstance(result, LLMResponse):
                return result
            text = str(result)
        else:
            text = str(self.response)

        return LLMResponse(
            text=text,
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            # ``latency_ms`` は configured latency を採用する。実測ではなく
            # 「テスト意図の値」を Event_Log まで伝搬させるため、テスト側
            # から SLA アサーションを書きやすい。
            latency_ms=int(self.latency * 1000),
            model=chosen_model,
        )


def make_timeout_provider(timeout_seconds: float = 70.0) -> FakeLLMProvider:
    """60 秒タイムアウトを誘発する :class:`FakeLLMProvider` を構築する。

    REQ 3.4 (60 秒) と REQ 3.5 (キャンセル後 1 秒以内のエラー伝達) の
    SLA テストで使う。caller は本 Provider を
    ``asyncio.wait_for(provider.invoke(...), 60)`` でラップすることで、
    ``asyncio.TimeoutError`` を発生させ、:class:`vsm.errors.LLMTimeoutError`
    に変換するパス (Task 10.1: ``SubAgent.respond``) を駆動できる。

    Parameters
    ----------
    timeout_seconds : float
        ``latency`` に設定する秒数。既定値 70.0 は 60 秒タイムアウトを
        確実に超過するためのマージン込み値。
    """
    return FakeLLMProvider(latency=timeout_seconds)


def make_error_provider(
    code: int | str = 500,
    message: str = "internal error",
) -> FakeLLMProvider:
    """常に :class:`LLMProviderError` を raise するフェイクを構築する。

    REQ 3.6 (プロバイダーエラー時の typed-error 伝達) の単体 / PBT
    テストで使う。``code`` はプロバイダーの HTTP / SDK エラーコード
    (例: ``500``, ``"rate_limit"``) を想定。

    Parameters
    ----------
    code : int | str
        :class:`LLMProviderError.code` に渡される値。既定は 500。
    message : str
        :class:`LLMProviderError.message` に渡される値。
    """
    return FakeLLMProvider(error=LLMProviderError(code=code, message=message))
