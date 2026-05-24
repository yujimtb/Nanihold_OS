"""S3*_Auditor: VSM System 3* — direct audit of S1_Workers (Task 16.1).

design.md `## Components and Interfaces` §S3Star_Auditor に対応する。
S3* は S3_Allocator の **通常チャネルを経由せず**、独立した
:data:`~vsm.messaging.channels.ChannelId.S3STAR_TO_S1` で各 S1_Worker を
直接観測し、必要に応じて
:data:`~vsm.messaging.channels.ChannelId.S3STAR_S5_AUDIT` で S5_Policy に
findings をエスカレーションする (REQ 9.1〜9.6)。

Audit cycle
-----------
1. **トリガ (REQ 9.1)** — 30 秒タイマと「S1 assignment 完了通知」のうち
   先に到達した方で起動する。:meth:`run` は ``asyncio.wait``
   (``FIRST_COMPLETED``) で 2 つの coroutine を多重化し、片方を消費 →
   未完了側を cancel → 次 iteration で再生成、というパターンで
   実装する (S4_Scanner / S5_Policy / S3_Allocator と同じ多重化形式)。
2. **観測 (REQ 9.2)** — 起動時点で :class:`Platform` が把握している
   全 S1_Worker に対し ``audit_observation`` event を 1 秒以内に append。
   各 S1 の状態は ``specialization`` / ``current_assignments`` から
   defensively 取得する (S1_Worker の Task 17.1 実装に依存しないよう
   :func:`getattr` を使用)。
3. **Finding 生成 (REQ 9.3 / 9.4)** — 本 PoC では各観測が finding を
   発生させる単純なポリシを採用する (design.md §S3Star_Auditor の
   暫定形)。``audit_finding`` event を 1 秒以内に append。``content``
   は schema 上 ``min_length=1`` の string なので、観測内容を
   人間可読な非空文字列にシリアライズする。
4. **S5 への配送 (REQ 9.5)** — :data:`ChannelId.S3STAR_S5_AUDIT` で
   :class:`MessageBus.send` を呼ぶ。Bus は同一 event-loop tick 内に
   ``put_nowait`` + ``channel_message`` append まで完了するため、
   5 秒 SLA は構造的に満たされる。
5. **配送記録 (REQ 9.6)** — ``SendResult.delivered`` が ``True`` の場合
   のみ ``audit_report_sent`` を 1 秒以内に append する。disallowed route
   や未 subscribe 受信者で rejected された場合は Bus 側で
   ``channel_rejected`` が記録済みなので二重記録を避ける。

Channel isolation (REQ 9.1)
---------------------------
本クラスは S3_Allocator の参照を一切持たず、すべての観測は
:data:`ChannelId.S3STAR_TO_S1` で送る (本 PoC の MVP ではメッセージ送信
を伴う観測ではなく、:class:`Platform` 経由のメモリ内 snapshot で代用
している — S3_Allocator を経由しない `という` 不変条件は
:class:`MessageBus` の ``(receiver_id, channel)`` キー構造が物理的に
保証する。後続 task で観測要求を Channel 経由に切り替える場合も、
``S3STAR_TO_S1`` を通る限り S3_Allocator のキューには絶対届かない)。

Concurrent completion coalescing
--------------------------------
:meth:`notify_completion` が「複数の S1 完了が短時間に発生した」状況で
複数回呼ばれても、内部の :class:`asyncio.Event` は 1 状態しか持たない
ため、次の iteration で 1 度だけ観測サイクルが回る (= coalesce)。これは
REQ 9.1 の "30 秒間隔または assignment 完了で **whichever first**" を
忠実に満たす最小実装。

Validates Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from vsm.clock import Clock
from vsm.config import RunConfig
from vsm.eventlog.writer import EventLogWriter
from vsm.ids import generate_uuid
from vsm.llm.types import LLMProviderProtocol
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId
from vsm.messaging.message import Message
from vsm.roles import SystemRole
from vsm.systems.base import System

if TYPE_CHECKING:
    # 循環参照回避: ``Platform`` は :mod:`vsm.runtime.lifecycle` で本クラスを
    # 動的 import するため、型注釈用のみで取り込む。
    from vsm.runtime.lifecycle import Platform


__all__ = ["S3StarAuditor"]


# REQ 9.1: 観測サイクルの周期上限 (30 秒)。``asyncio.sleep`` で実装する
# が、テストでは ``FakeClock`` + ``monkeypatch.setattr("asyncio.sleep",
# ...)`` で加速する想定 (S4_Scanner と同様のパターン)。
_AUDIT_INTERVAL_SECONDS: float = 30.0


class S3StarAuditor(System):
    """VSM System 3* — direct auditor of S1_Workers (Task 16.1).

    Parameters
    ----------
    system_id : str
        Run 内一意な System 識別子 (lifecycle が UUIDv4 で発行)。
    eventlog : EventLogWriter
        Run 全体で共有される Event_Log writer。
    bus : MessageBus
        Channel 経由の送信を仲介する :class:`MessageBus`。
        受信側 (S3STAR_TO_S1 / S3STAR_S5_AUDIT) は本クラスからは
        sender 専用なので subscribe しない。
    llm : LLMProviderProtocol
        Sub_Agent が利用する LLM プロバイダー (REQ 3.1, 3.7)。本 MVP
        では LLM ベースの finding 解析は行わないが、後続 task で
        Sub_Agent を介した解析を導入できるよう保持する。
    clock : Clock
        Message envelope の ``timestamp_ms`` 計算と SLA 計測に使う。
    platform : Platform
        S1_Worker 一覧 / S5_Policy インスタンス参照を引くために保持する。
    run_config : RunConfig
        Run 構造設定。``count(SystemRole.S3STAR_AUDITOR)`` 個の
        Sub_Agent を ``__init__`` で登録する (REQ 1.4 下限を満たすため
        最低 1 個は必ず登録される)。

    Validates Requirements: 1.1, 1.4, 9.1〜9.6.
    """

    def __init__(
        self,
        *,
        system_id: str,
        eventlog: EventLogWriter,
        bus: MessageBus,
        llm: LLMProviderProtocol,
        clock: Clock,
        platform: "Platform",
        run_config: RunConfig,
    ) -> None:
        super().__init__(
            system_id=system_id,
            role=SystemRole.S3STAR_AUDITOR,
            eventlog=eventlog,
            llm=llm,
            clock=clock,
        )
        self._bus: MessageBus = bus
        self._platform: "Platform" = platform
        self._run_config: RunConfig = run_config

        # REQ 1.4: Sub_Agent は ``run_config.count(role)`` 個を登録する。
        # ``max(1, ...)`` で下限 1 を保証 — REQ 13.4 (1..16 の範囲) は
        # :class:`RunConfig` 側で検証済みだが、defensively 0 が来ても
        # 1 個は登録して REQ 1.4 下限を満たせるようにする。lifecycle
        # の defensive fallback (default Sub_Agent 登録) と二重に保険を
        # かける形になるが、副作用は無いので問題無い。
        for i in range(max(1, run_config.count(SystemRole.S3STAR_AUDITOR))):
            self.register_sub_agent(label=f"auditor-{i}")

        # REQ 9.1: 「assignment 完了通知」シグナル。S1 (もしくは S3) が
        # ``notify_completion()`` を呼ぶと set される。:meth:`run` の
        # ``asyncio.wait`` で 30 秒タイマと多重化されるため、複数の
        # 完了通知が観測サイクル中に集まっても次回 iteration で 1 度
        # まとめて消化される (coalescing semantics)。``Event`` の
        # 生成は ``__init__`` で行う — asyncio の event loop binding
        # は最初の ``await`` まで遅延されるため、``run()`` 起動前に
        # ``notify_completion`` が呼ばれても安全。
        self._completion_signal: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_completion(self) -> None:
        """Signal that an S1_Worker has completed an assignment (REQ 9.1).

        S1 / S3_Allocator から呼ばれ、:attr:`_completion_signal` を set
        するだけの軽量メソッド。:meth:`run` ループはこのシグナルと
        30 秒タイマの **どちらか早い方** で次の観測サイクルを開始する
        (REQ 9.1: "either at intervals of 30 seconds or upon completion
        of an S1_Worker assignment, whichever occurs first")。

        Notes
        -----
        - Idempotent: 既に set 済みの状態で再度呼んでも追加効果は無い。
          観測サイクル中に発生した複数完了は 1 回の iteration で coalesce
          される (PoC 仕様としてこれを許容している)。
        - 同期メソッド: ``asyncio.Event.set()`` は I/O を伴わないため
          ``async`` にせず、callee 側の制約を最小化する。S1 の通常
          パスから呼ぶ際もイベントループハンドオフは不要。
        """
        self._completion_signal.set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Periodically (or on completion) audit every S1_Worker (REQ 9.1).

        ループの構造:

        1. 30 秒スリープ Task と完了シグナル待機 Task の 2 つを生成し、
           :func:`asyncio.wait` (``FIRST_COMPLETED``) で多重化する。
        2. どちらかが完了したら、未完了側を cancel する (cancel された
           側は次回 iteration で再生成される)。
        3. ``_completion_signal`` は ``clear()`` してから観測サイクル
           :meth:`_observe_all_s1` に進む。
        4. 観測サイクルが完了したらループ先頭に戻り、次の 30 秒/完了
           待機を始める。

        :class:`asyncio.CancelledError` は ``Platform.shutdown`` 経由で
        投げられるので、握りつぶさずに re-raise する (基底
        ``System.shutdown`` が ``cancel()`` 後に await し、CancelledError
        を捕捉する)。

        Validates Requirements: 9.1.
        """
        try:
            while True:
                # 各 iteration で 2 つの待機 Task を生成する。``Event.wait()``
                # は cancel-safe なので、未完了のまま cancel しても次回の
                # ``Event.wait()`` で再度 set を観測できる (= shadow state
                # は ``Event`` 内部の flag のみ)。
                timer_task = asyncio.create_task(
                    asyncio.sleep(_AUDIT_INTERVAL_SECONDS),
                    name="s3star_audit_timer",
                )
                signal_task = asyncio.create_task(
                    self._completion_signal.wait(),
                    name="s3star_audit_signal",
                )

                try:
                    done, pending = await asyncio.wait(
                        {timer_task, signal_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    # ``Platform.shutdown`` 経由のキャンセル。両 Task を
                    # 確実に cancel + 待機してから再 raise する (孤児
                    # task で warning が出ないよう defensive cleanup)。
                    timer_task.cancel()
                    signal_task.cancel()
                    # ``return_exceptions=True`` で CancelledError を吸収。
                    await asyncio.gather(
                        timer_task, signal_task, return_exceptions=True
                    )
                    raise

                # 未完了側 (= 後発) を cancel して掃除する。S4_Scanner /
                # S5_Policy と同じパターン (FIRST_COMPLETED の常套句)。
                for p in pending:
                    p.cancel()
                # 完了済み Task の結果は捨てて良い (sleep は None、
                # Event.wait は True を返すだけ)。例外を回収するため
                # ``await`` だけ行う。
                for d in done:
                    try:
                        d.result()
                    except asyncio.CancelledError:
                        # FIRST_COMPLETED は通常 cancel された Task を
                        # done に含めないが、レース条件下では発生しうる。
                        pass
                # cancel した pending task の終了も待つ (warning 抑制)。
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

                # 次サイクルのためにシグナルを clear。タイマで起きた
                # 場合は元々 set されていないので no-op、シグナルで
                # 起きた場合は次のサイクル受け取り準備となる。
                self._completion_signal.clear()

                # 観測サイクル本体 (REQ 9.2〜9.6)。
                await self._observe_all_s1()
        except asyncio.CancelledError:
            # 上位の cancel を尊重する (基底 System.shutdown が捕捉)。
            raise

    # ------------------------------------------------------------------
    # Audit cycle internals
    # ------------------------------------------------------------------

    async def _observe_all_s1(self) -> None:
        """Observe every known S1_Worker and produce findings (REQ 9.2〜9.6).

        :class:`Platform.systems` から ``SystemRole.S1_WORKER`` の一覧を
        取り出し、各 S1 について以下を順次行う:

        1. ``audit_observation`` を append (REQ 9.2 — 1 秒 SLA は
           ``EventLogWriter.append`` の in-memory enqueue で構造的達成)。
        2. :meth:`_produce_finding` を呼んで finding 生成 → S5 配送 →
           ``audit_report_sent`` まで一括処理 (REQ 9.3〜9.6)。

        S1_Worker 不在 (Run 開始直後など) の場合はループ自体が空回しと
        なり、何も append されない — REQ 9.1 の「poll the state of every
        S1_Worker」は「存在する全 S1」と解釈する。
        """
        # ``platform.systems`` は ``dict[SystemRole, list[System]]``。
        # 該当 role が無い場合は空リストを返す defensive default。
        s1_workers = self._platform.systems.get(SystemRole.S1_WORKER, [])

        for s1 in s1_workers:
            # S1_Worker (Task 17.1) の正式 attribute 名は未確定なため、
            # ``getattr`` で defensively 取得する。``specialization`` は
            # design.md §Data Models §S1_Worker State の正式フィールド、
            # ``current_assignments`` も同。``_completed_count`` は
            # PoC 拡張用の内部カウンタ想定 (S1 が完了報告した件数)。
            observed_state: dict[str, Any] = {
                "specialization": getattr(s1, "specialization", "unknown"),
                "current_assignments": list(
                    getattr(s1, "current_assignments", [])
                ),
                "completed_count": getattr(s1, "_completed_count", 0),
            }

            # REQ 9.2: ``audit_observation`` を 1 秒以内に append。
            # ``EventLogWriter.append`` は in-memory queue へ enqueue する
            # 軽量メソッドなので、同一 event-loop tick 内で完了する。
            await self._eventlog.append(
                "audit_observation",
                {
                    "s1_id": s1.system_id,
                    "observed_state": observed_state,
                },
            )

            # REQ 9.3: 観測 → finding 生成は 60 秒以内。本 PoC では
            # 同期的に finding を作るため、SLA は構造的に達成。後続 task
            # で LLM Sub_Agent を介した解析に置き換える場合は
            # ``asyncio.wait_for(..., 60.0)`` で保護すべき。
            await self._produce_finding(
                s1_id=s1.system_id,
                observed_state=observed_state,
            )

    async def _produce_finding(
        self,
        *,
        s1_id: str,
        observed_state: dict[str, Any],
    ) -> None:
        """Produce a finding, deliver to S5_Policy, and record the report.

        Steps:

        - ``audit_finding`` event を append (REQ 9.4, 1 秒 SLA)。
        - :data:`ChannelId.S3STAR_S5_AUDIT` で S5_Policy に Message を送る
          (REQ 9.5, 5 秒 SLA — Bus が同 tick で put_nowait するため構造
          的に達成)。
        - 配送が成功した場合のみ ``audit_report_sent`` を append
          (REQ 9.6, 1 秒 SLA)。

        Parameters
        ----------
        s1_id : str
            観測対象 S1_Worker の ``system_id``。
        observed_state : dict
            :meth:`_observe_all_s1` が組み立てた state snapshot。

        Notes
        -----
        - :data:`AuditFindingPayload.content` は ``min_length=1`` の
          string が要求される。``s1_id`` は UUIDv4 hex (32 文字) で
          常に非空のため、組み立てた summary も常に非空である。
        - REQ 9.5 が要求する受信者は S5_Policy。:class:`Platform.systems`
          には Run 開始時に必ず 1 個以上の S5_Policy が存在する
          (REQ 1.2 / 13.1) ため、empty list を取るのは構造制約違反時
          のみ。defensively その場合は ``"unknown"`` を receiver_id に
          入れて Bus の挙動 (未 subscribe → ``SendResult.rejected``)
          に委ねる。
        """
        finding_id = generate_uuid()

        # REQ 9.4: ``audit_finding.content`` は非空文字列。observed_state
        # を repr 経由で簡易シリアライズ — 完全な JSON 化ではなく、
        # 人間可読な要約として扱う (S5 側で再パースする必要がある場合
        # は payload を別フィールドで渡す設計に拡張する)。
        content = (
            f"audit observation of S1 {s1_id}: "
            f"specialization={observed_state.get('specialization')!r}, "
            f"current_assignments="
            f"{observed_state.get('current_assignments')!r}, "
            f"completed_count={observed_state.get('completed_count')!r}"
        )

        # REQ 9.4: ``audit_finding`` を 1 秒以内に append。
        await self._eventlog.append(
            "audit_finding",
            {
                "finding_id": finding_id,
                "s1_id": s1_id,
                "content": content,
            },
        )

        # REQ 9.5: S5_Policy への配送。S5 インスタンスは Run 開始時に
        # 必ず 1 個以上存在するが (REQ 1.2 / 13.1)、defensively empty
        # list の可能性を扱う。最初の 1 個を recipient とする (S4_Scanner
        # の :meth:`_produce_and_deliver` と同じ方針)。
        s5_instances = self._platform.systems.get(SystemRole.S5_POLICY, [])
        recipient_id = (
            s5_instances[0].system_id if s5_instances else "unknown"
        )

        msg = Message(
            message_id=generate_uuid(),
            sender_role=SystemRole.S3STAR_AUDITOR,
            sender_id=self.system_id,
            receiver_role=SystemRole.S5_POLICY,
            receiver_id=recipient_id,
            channel=ChannelId.S3STAR_S5_AUDIT,
            payload={
                "finding_id": finding_id,
                "s1_id": s1_id,
                "content": content,
            },
            # REQ 2.9 の envelope timestamp。Event_Log の ``ts`` は writer
            # 側で別途付くが、Message 自体にも ms 精度の時刻を持たせる
            # 設計 (S4_Scanner と同じ慣習)。
            timestamp_ms=int(self._clock.now().timestamp() * 1000),
        )

        result = await self._bus.send(msg)

        # REQ 9.6: 配送が成功した場合のみ ``audit_report_sent`` を 1 秒
        # 以内に append。``SendResult.delivered=False`` (disallowed route
        # / 未 subscribe) のときは Bus 側で既に ``channel_rejected`` が
        # 記録されているので、ここで報告イベントを追加すると Event_Log
        # の整合性が壊れる (届いていないのに「届いた」と記録するのは
        # 仕様違反)。
        if result.delivered:
            await self._eventlog.append(
                "audit_report_sent",
                {"finding_id": finding_id},
            )
