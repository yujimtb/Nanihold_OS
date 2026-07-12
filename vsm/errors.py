"""VSM_Platform 例外階層。

design.md `## Error Handling` の §例外階層 と §Exit Code 体系 に対応する
全例外をここで集中管理する。すべての例外は :class:`VSMError` のサブクラス
であり、System 間に伝播する際は ``payload`` に ``error_type`` / ``detail``
を含める前提のため、本モジュールでは追加の引数 (e.g. ``missing_roles``,
``exit_code``, ``code`` など) を保持できる構造のみを提供する。

Exit Code 体系 (design.md §Exit Code 体系):

==== =====================================================================
code 意味
==== =====================================================================
0    正常終了
1    内部例外 (未分類)
2    CLI 入力バリデーション違反 (REQ 4.2, 4.5, 10.2, 11.7)
3    構造制約違反 / 必須 System 不足 (REQ 13.2)
4    Run ディレクトリ / Event_Log 作成失敗 (REQ 10.4)
5    スコープ外機能要求 (REQ 14.8)
6    代表シナリオの 1800 秒タイムアウト (REQ 12.9)
==== =====================================================================
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


class VSMError(Exception):
    """全 VSM_Platform 例外の基底。"""


# ---------------------------------------------------------------------------
# Configuration / CLI / Runtime bootstrap
# ---------------------------------------------------------------------------


class ConfigError(VSMError):
    """設定読み込み / 構造検証失敗。

    REQ 13.2, 13.3: 必須 System 不足を含む構造制約違反は本例外で表現し、
    ``missing_roles`` に欠落した役割名 (例: ``["S2_COORDINATOR"]``) を、
    ``detail`` に人間可読な追加情報を保持する。
    """

    def __init__(self, missing_roles: list[str], detail: str) -> None:
        self.missing_roles: list[str] = list(missing_roles)
        self.detail: str = detail
        if self.missing_roles:
            message = f"{detail} (missing_roles={self.missing_roles})"
        else:
            message = detail
        super().__init__(message)


class CLIError(VSMError):
    """CLI 入力バリデーション失敗。

    REQ 4.2, 4.5, 10.2, 11.7, 14.8: CLI レイヤで検出した違反は ``exit_code``
    を保持して raise され、エントリポイントが ``sys.exit(exit_code)`` で
    プロセスを終了させる。``exit_code`` の既定値は ``1`` (内部例外, 未分類)
    だが、CLI バリデーションでは通常 ``2``、スコープ外要求では ``5`` を
    与える (design.md §Exit Code 体系)。
    """

    def __init__(self, message: str, exit_code: int = 1) -> None:
        self.exit_code: int = exit_code
        super().__init__(message)


class RunDirectoryError(VSMError):
    """``runs/{run_id}`` ディレクトリ / ``events.jsonl`` 作成失敗。

    REQ 10.4: 作成に失敗した場合 stderr にメッセージを書き、CLI は
    exit code 4 でプロセスを終了させる。
    """


class WorkspaceError(VSMError):
    """self-hosting の manifest / git worktree 操作に失敗した。"""


class WorkspacePolicyError(WorkspaceError):
    """manifest が許可していないパスに変更があった。"""


class GateError(VSMError):
    """信頼済み GateRunner の入力または検証に失敗した。"""


class CandidateCommitError(VSMError):
    """候補 commit の完全性検証または作成に失敗した。"""


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------


class MessagingError(VSMError):
    """Message_Bus 関連エラーの基底。"""


class ChannelRejected(MessagingError):
    """``ALLOWED_ROUTES`` に存在しない経路が要求された。

    REQ 2.7: Message_Bus は未定義チャネルへの送信を拒否する。Bus 自体は
    送信元へ非例外的に ``SendResult.delivered=False`` を返す設計である
    ため、本クラスは主に payload / 診断ロジックでの型付けに用いる。
    """


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


class LLMError(VSMError):
    """LLM 呼び出し関連エラーの基底。"""


class LLMTimeoutError(LLMError):
    """REQ 3.5: LLM 呼び出しが 60 秒タイムアウトを超過した。

    ``sub_agent_id`` で発火元 Sub_Agent を識別し、``elapsed`` は経過時間
    (``timedelta`` または float 秒) を保持する。caller (``SubAgent.respond``)
    は本例外を捕捉して ``llm_timeout`` event を Event_Log に append し、
    1 秒以内に上位ループへ伝達する。
    """

    def __init__(
        self,
        sub_agent_id: str,
        elapsed: timedelta | float,
    ) -> None:
        self.sub_agent_id: str = sub_agent_id
        self.elapsed: timedelta | float = elapsed
        if isinstance(elapsed, timedelta):
            elapsed_s = elapsed.total_seconds()
        else:
            elapsed_s = float(elapsed)
        super().__init__(
            f"LLM invocation timed out after {elapsed_s:.3f}s "
            f"(sub_agent_id={sub_agent_id})"
        )


class LLMProviderError(LLMError):
    """REQ 3.6: LLM プロバイダー側のエラー。

    ``code`` はプロバイダーの HTTP / SDK エラーコード、``message`` は
    プロバイダーが返したメッセージ本文。``llm_error`` event の
    ``provider_code`` / ``provider_message`` payload にそのまま転写される。
    """

    def __init__(self, code: int | str, message: str) -> None:
        self.code: int | str = code
        self.message: str = message
        super().__init__(f"LLM provider error [{code}]: {message}")


class BudgetExceededError(LLMError):
    """AgentRuntime 呼び出し前に Node または Run 予算が尽きていた。"""


class QuotaExhaustedError(LLMError):
    """AgentRuntime の quota 枯渇により Node が休眠へ移行した。"""


# ---------------------------------------------------------------------------
# Event_Log
# ---------------------------------------------------------------------------


class EventLogError(VSMError):
    """Event_Log 関連エラーの基底。"""


class EventLogAppendError(EventLogError):
    """REQ 10.6: 100 ms 間隔で 3 回リトライしても append に失敗した。

    ``event`` は append しようとしたイベント (``vsm.eventlog.schema.Event``
    型を意図するが、循環 import を避けるため本モジュールでは ``Any``
    として保持する)。``cause`` は基底となった ``OSError``。
    """

    def __init__(self, event: Any, cause: OSError) -> None:
        self.event: Any = event
        self.cause: OSError = cause
        event_type = getattr(event, "event_type", type(event).__name__)
        super().__init__(
            f"failed to append event_type={event_type!r} after retries: {cause}"
        )
        self.__cause__ = cause


# ---------------------------------------------------------------------------
# System lifecycle / dispatch / coordination
# ---------------------------------------------------------------------------


class SystemInstantiationError(VSMError):
    """REQ 1.7, 7.5: 必須 System または S1_Worker の生成に失敗した。

    Run 開始前 (REQ 1.7) は致命的失敗として exit code 3 で終了し、
    Run 中の S1 動的生成失敗 (REQ 7.5) は ``s1_instantiation_error``
    event の append + 5 秒以内の S5 通知で扱う。
    """


class DispatchError(VSMError):
    """REQ 6.5: S5 並行ディスパッチの片側が失敗した。

    例外は送信側で握りつぶし、``dispatch_error`` event を 1 秒以内に
    append しつつ、もう片方 (S3 / S4) の処理は継続する。
    """


class SubAgentError(VSMError):
    """REQ 5.5: Sub_Agent が個別タイムアウト (30 秒) 等で失敗した。

    呼び出し元は本例外を捕捉して ``sub_agent_error`` event を append し、
    残りの Sub_Agent で処理を継続する。
    """


class CoordinationAckMissing(VSMError):
    """REQ 8.6: S2_Coordinator の directive に対する ack が 30 秒不達。

    S2 は本例外相当の状況を検出すると ``coordination_ack_missing`` event
    を append する。
    """


__all__ = [
    "VSMError",
    "ConfigError",
    "CLIError",
    "RunDirectoryError",
    "WorkspaceError",
    "WorkspacePolicyError",
    "GateError",
    "CandidateCommitError",
    "MessagingError",
    "ChannelRejected",
    "LLMError",
    "LLMTimeoutError",
    "LLMProviderError",
    "BudgetExceededError",
    "QuotaExhaustedError",
    "EventLogError",
    "EventLogAppendError",
    "SystemInstantiationError",
    "DispatchError",
    "SubAgentError",
    "CoordinationAckMissing",
]
