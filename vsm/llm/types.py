"""LLM_Provider_Abstraction の値オブジェクトとプロトコル定義 (Task 9.1).

design.md `## Components and Interfaces` §4 (LLM_Provider_Abstraction) と
§パッケージレイアウト (``vsm/llm/types.py``) に対応する。本モジュールは
``litellm`` を import しないため、``FakeLLMProvider`` (Task 9.2) や
プロトコル参照だけが必要なテスト・型定義から軽量に取り込める。

Requirements traced
-------------------
- REQ 3.1: ``LLMProviderProtocol`` で実装差し替えポイントを明示する。
- REQ 3.3: ``LLMResponse`` は Event_Log の ``llm_invocation`` payload
  (model, prompt, response, latency_ms, tokens_in, tokens_out) に直接
  写像できるフィールドを持つ。
- REQ 3.4: 60 秒タイムアウトの計測対象は ``latency_ms`` として保持する。
- REQ 3.6: プロバイダーエラー時は ``LLMProviderError`` (vsm.errors) に
  変換するため、本モジュールでは正常応答の構造のみを定義する。
- REQ 3.7: ``LLMRequest.model`` を ``None`` で明示的に「Provider の既定
  モデルを使う」と表現でき、System / Sub_Agent コードを変更せず
  プロバイダー差し替えが可能になる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


__all__ = [
    "LLMRequest",
    "LLMResponse",
    "LLMProviderProtocol",
]


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """単一 LLM 呼び出しのリクエスト値オブジェクト。

    Attributes
    ----------
    prompt : str
        Sub_Agent から LLM に渡すユーザープロンプト本文。
    model : str | None
        ``None`` の場合は Provider の既定モデル
        (``LLMConfig.resolve_model()``) が使われる (REQ 3.7)。
        テスト等で明示的に切り替えたい場合のみ非 ``None`` を指定する。
    metadata : dict[str, Any]
        Event_Log payload や診断ログに転写する補助情報を保持するための
        オープンな辞書。``frozen`` だが、``field(default_factory=dict)``
        により参照は一意なので呼び出し側がたまたま共有することはない。
    """

    prompt: str
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """単一 LLM 呼び出しのレスポンス値オブジェクト。

    フィールド構成は design.md §4 のサンプル実装と REQ 3.3 が要求する
    Event_Log payload (``llm_invocation``) に対応する。

    Attributes
    ----------
    text : str
        モデルが生成した応答本文。``choices[0].message.content`` を採用。
    tokens_in : int
        入力 (prompt) トークン数。``usage.prompt_tokens``。
    tokens_out : int
        出力 (completion) トークン数。``usage.completion_tokens``。
    latency_ms : int
        本リクエストの所要時間 (ミリ秒)。Provider の自己申告値が利用
        できない場合は呼び出し側で ``time.monotonic`` 差分を採用する。
    model : str
        実際に使われたモデル文字列 (例: ``"openai/gpt-4o-mini"``)。
        Event_Log のトレーサビリティ要件のため、Request 側の ``None``
        ではなく解決済みの実モデル名を保持する。
    """

    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    model: str


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """LLM Provider が満たすべき非同期インターフェース。

    本番実装は :class:`vsm.llm.provider.LLMProvider` (LiteLLM ラッパ)、
    テストでは :class:`vsm.llm.fake.FakeLLMProvider` (Task 9.2) で差し
    替え可能とする (REQ 3.1, 3.7)。``runtime_checkable`` を付与している
    ため、PBT 等で ``isinstance(provider, LLMProviderProtocol)`` を用いた
    軽量な型チェックも可能。
    """

    async def invoke(self, prompt: str, model: str | None = None) -> LLMResponse:
        """``prompt`` を LLM に送信して :class:`LLMResponse` を返す。

        Parameters
        ----------
        prompt : str
            ユーザープロンプト本文。
        model : str | None
            ``None`` の場合は Provider の既定モデルを使う (REQ 3.7)。
        """
        ...
