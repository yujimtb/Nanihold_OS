"""S1_Worker: VSM System 1 — environment-facing worker (Task 17.1).

design.md `## Components and Interfaces` §S1_Worker (vsm/systems/s1_worker.py)
の実装。S3_Allocator から S1_S3 で受け取った assignment を Sub_Agent
(LLM) で実行し、完了を S1_S3 で報告する。並行して S2_Coordinator から
S1_S2 で受け取った coordination directive を 1 秒以内に ack 返送し、
S3*_Auditor からの観測要求 (S3STAR_TO_S1) は PoC では受信のみ行う
(simplification: S3*_Auditor は ``platform.systems`` を直接参照するため、
本 channel への明示的な応答は不要)。

Lifecycle role
--------------
本クラスは Run start 時には生成されず、:meth:`vsm.runtime.lifecycle.Platform.spawn_s1`
が S3_Allocator の reuse / instantiate 判断に基づいて動的に生成する
(REQ 1.6, 7.3, 13.6)。生成時に ``s1_instantiated`` event と
``system_instantiated`` event は Platform 側が append するため、本クラスの
``__init__`` 内では event を発行しない。

Constructor contract (lifecycle)
--------------------------------
キーワード専用引数のみを受ける。``specialization`` は S2_Coordinator の
conflict 検出 (REQ 8.2 — 同 specialization + 同 work_item_id の二重保持
を検出) と S3_Allocator の reuse 判定 (REQ 7.2 — idle = 同 specialization
かつ ``current_assignments`` 空) に直接使われる属性であり、外部から
読み取られることが前提のため public 属性として保持する。同様に
``current_assignments`` も public 属性として保持し、S3_Allocator が
:meth:`_assign_work` で append、本クラスが完了時に remove する。

Sub_Agent registration
----------------------
``register_sub_agent(label=f"{specialization}-default")`` で 1 個の
Sub_Agent を登録する (REQ 1.4 下限)。RunConfig の S1_WORKER count
(REQ 13.5 で 0 許容、REQ 1.4 / 13.6 で上限 64) は Run start 時のみの
意味で、動的生成 S1 は最低限 1 個の Sub_Agent を保持すれば良い。

Channels
--------
* **入力**:

  - ``S1_S3`` (S3_Allocator から assignment 受信)
  - ``S1_S2`` (S2_Coordinator から coordination directive 受信)
  - ``S3STAR_TO_S1`` (S3*_Auditor から観測要求受信; PoC では no-op)

* **出力**:

  - ``S1_S3`` (S3_Allocator へ完了 / 失敗報告)
  - ``S1_S2`` (S2_Coordinator へ ack 返送)

Validates Requirements
----------------------
- REQ 1.6: 動的生成された S1 の ``system_instantiated`` event は
  :meth:`Platform.spawn_s1` が発行する (本クラスでは二重発行しない)。
- REQ 7.7: S1_S3 で assignment を受信したら ``current_assignments`` に
  追加し、Sub_Agent (LLM) で実行し、完了を S1_S3 で報告する。
- REQ 7.8: 完了報告は ``s1_completion`` event の append + S1_S3 経由で
  S3_Allocator に送信する (S3 側は受信から 5 秒以内に S5 へ転送)。
- REQ 8.5: S1_S2 で directive を受信したら 1 秒以内に S1_S2 で ack を
  返送する (構造的保証 — 単一 ``MessageBus.send`` で完結)。
- REQ 9.1 (受領側): S3STAR_TO_S1 から観測要求を受信した場合に対応する
  受信ループを持つ。PoC では simplification として明示的な応答を返さず、
  S3*_Auditor が ``platform.systems`` から状態を直接読む設計を採用する。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from vsm.ids import generate_uuid
from vsm.memory import SearchScope, TaskSummary
from vsm.messaging.channels import ChannelId
from vsm.messaging.message import Message
from vsm.roles import SystemRole
from vsm.systems.base import System
from vsm.tools.search import IndexedTaskSummary

if TYPE_CHECKING:
    # 型注釈のみで使い、実行時の循環 import を避ける。
    from vsm.clock import Clock
    from vsm.config import RunConfig
    from vsm.eventlog.writer import EventLogWriter
    from vsm.agents.runtime import AgentRuntimeProtocol
    from vsm.messaging.bus import MessageBus
    from vsm.runtime.lifecycle import Platform


__all__ = ["S1Worker"]


class S1Worker(System):
    """VSM System 1 — environment-facing worker.

    1 つの ``specialization`` (例: ``"frontend"``, ``"test"``,
    ``"backend"``) に紐付き、S3_Allocator から渡される work item を
    Sub_Agent (LLM) で実行する。

    Attributes
    ----------
    specialization : str
        S1 の専門化ラベル。S2 conflict 検出 (REQ 8.2) と S3 reuse
        判定 (REQ 7.2) に使われる public 属性。
    current_assignments : list[str]
        現在保持している ``work_item_id`` のリスト。S3 が新規 assignment
        を append し、本クラスが完了時に remove する (REQ 7.2 idle 定義の
        基準)。

    Validates Requirements: 1.6, 7.7, 7.8, 8.5, 9.1.
    """

    def __init__(
        self,
        *,
        system_id: str,
        eventlog: "EventLogWriter",
        bus: "MessageBus",
        runtime: "AgentRuntimeProtocol | None",
        clock: "Clock",
        platform: "Platform",
        run_config: "RunConfig",
        specialization: str,
    ) -> None:
        super().__init__(
            system_id=system_id,
            role=SystemRole.S1_WORKER,
            eventlog=eventlog,
            runtime=runtime,
            clock=clock,
        )
        self._bus = bus
        self._platform = platform
        self._run_config = run_config
        # public: S2 conflict 検出 / S3 reuse 判定で外部から読まれる。
        self.specialization: str = specialization
        # public: S3_Allocator._assign_work が append、本クラスの完了
        # 経路が remove する。空リストである間は idle (REQ 7.2)。
        self.current_assignments: list[str] = []
        # 内部統計。テストや診断ログでの完了件数追跡に使う。
        self._completed_count: int = 0
        # REQ 1.4: 最低 1 個の Sub_Agent を登録する。``specialization``
        # ラベルを Sub_Agent ラベルにも転写し、Event_Log の診断時に
        # どの S1 がどの専門化で動作したか追跡しやすくする。
        self.register_sub_agent(label=f"{specialization}-default")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Subscribe to S1_S3 / S1_S2 / S3STAR_TO_S1 and dispatch messages.

        3 つの受信チャネルを多重化する:

        * ``S1_S3`` — S3_Allocator からの assignment。
          :meth:`_execute_assignment` が Sub_Agent (LLM) で実行し、
          完了を Event_Log に記録した上で S1_S3 経由で S3 に報告する
          (REQ 7.7, 7.8)。
        * ``S1_S2`` — S2_Coordinator からの coordination directive。
          :meth:`_handle_directive` が 1 秒以内に ack を返送する
          (REQ 8.5)。
        * ``S3STAR_TO_S1`` — S3*_Auditor からの観測要求。PoC では受信
          ループに含めるのみで、明示的な応答は行わない (REQ 9.1
          simplification)。

        ``asyncio.wait`` で 3 キューを ``FIRST_COMPLETED`` で多重化し、
        :class:`asyncio.CancelledError` は ``System.shutdown`` 経路で
        伝播するよう pending タスクを明示的に cancel してから再 raise する。
        """
        q_s1_s3 = self._bus.subscribe(self.system_id, ChannelId.S1_S3)
        q_s1_s2 = self._bus.subscribe(self.system_id, ChannelId.S1_S2)
        q_audit = self._bus.subscribe(self.system_id, ChannelId.S3STAR_TO_S1)

        while True:
            t_s1_s3 = asyncio.create_task(q_s1_s3.get(), name="s1_worker.s1_s3")
            t_s1_s2 = asyncio.create_task(q_s1_s2.get(), name="s1_worker.s1_s2")
            t_audit = asyncio.create_task(q_audit.get(), name="s1_worker.s3star")
            tasks = {t_s1_s3, t_s1_s2, t_audit}
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                # shutdown 経路。pending をすべて cancel してから再 raise。
                for t in tasks:
                    t.cancel()
                raise

            for p in pending:
                p.cancel()

            for d in done:
                msg: Message = d.result()
                if (
                    msg.channel == ChannelId.S1_S3
                    and msg.sender_role == SystemRole.S3_ALLOCATOR
                ):
                    await self._execute_assignment(msg)
                elif (
                    msg.channel == ChannelId.S1_S2
                    and msg.sender_role == SystemRole.S2_COORDINATOR
                ):
                    await self._handle_directive(msg)
                elif (
                    msg.channel == ChannelId.S3STAR_TO_S1
                    and msg.sender_role == SystemRole.S3STAR_AUDITOR
                ):
                    # PoC simplification: S3*_Auditor は platform.systems を
                    # 直接参照するため、観測要求への明示的な応答は不要。
                    # 受信のみ行いキューを空にする。
                    pass

    # ------------------------------------------------------------------
    # Assignment execution (REQ 7.7, 7.8)
    # ------------------------------------------------------------------

    async def _execute_assignment(self, msg: Message) -> None:
        """REQ 7.7 / 7.8: assignment を Sub_Agent で実行し完了を報告する。

        手順:

        1. ``msg.payload`` から ``work_item_id`` を取り出し、
           ``current_assignments`` に追加する (S3_Allocator が事前に
           append している場合は冪等にスキップ)。
        2. ``self._sub_agents[0]`` (REQ 1.4 で最低 1 個保証) で LLM
           実行を行う。:class:`SubAgent.respond` 内で 60 秒タイムアウト
           と Event_Log 経路 (``llm_invocation`` / ``llm_timeout`` /
           ``llm_error``) は基底クラスが扱う (REQ 3.3〜3.6)。
        3. 例外捕捉時は ``success=False`` の ``result`` を作って完了
           報告を続行する (S3 への報告は失敗ケースでも必要)。
        4. ``current_assignments`` から work_item_id を取り除き、
           ``s1_completion`` event を append (REQ 7.8)。
        5. S3_Allocator へ S1_S3 で完了報告を送信。S3 側は受信後 5 秒
           以内に S5 へ転送する責務 (REQ 7.8)。
        6. S3*_Auditor の ``notify_completion`` を呼び出す (REQ 9.1:
           completion 発生時に観測トリガを起こす)。S3*_Auditor が未
           実装または該当メソッドを持たない場合は no-op。

        Parameters
        ----------
        msg : Message
            S3_Allocator から S1_S3 で送信された assignment メッセージ。
            ``payload`` には ``work_item_id`` (str) と ``assignment``
            (dict) が含まれる前提。
        """
        # ``payload`` は dict 想定だが defensive に扱う。
        payload: dict[str, Any] = msg.payload if isinstance(msg.payload, dict) else {}
        work_item_id = payload.get("work_item_id")
        if not isinstance(work_item_id, str) or not work_item_id:
            # S3 から有効な work_item_id が来なかった場合は新規発行する
            # (Event_Log schema の min_length=1 を満たすため)。
            work_item_id = generate_uuid()
        if work_item_id not in self.current_assignments:
            self.current_assignments.append(work_item_id)

        # Sub_Agent (LLM) で実行。``self._sub_agents`` は private 参照だが、
        # ``sub_agents`` プロパティは毎回コピーを返すためホットパスでは
        # private を直接参照する (S3_Allocator も同様)。
        sub_agent = self._sub_agents[0]
        try:
            response = await sub_agent.respond(
                prompt=f"役割: S1 ({self.specialization})\n今回の指示: {payload.get('assignment', {})}",
            )
            result_text = response.text or "completed"
            success = True
        except Exception as exc:
            # LLMTimeoutError / LLMProviderError 等は基底 Sub_Agent.respond
            # が既に Event_Log に append 済み (REQ 3.5 / 3.6)。S1 としては
            # 失敗を S3 に報告して current_assignments を空にする。
            result_text = f"failed: {exc}"
            success = False

        # current_assignments から削除して idle 状態に戻す (REQ 7.2
        # idle 定義の前提条件)。
        if work_item_id in self.current_assignments:
            self.current_assignments.remove(work_item_id)
        self._completed_count += 1

        # REQ 7.8: ``s1_completion`` event を append。schema は
        # ``result: dict[str, Any]`` を要求するため、success/text を
        # 含む dict として正規化する。
        result_payload: dict[str, Any] = {"success": success, "text": result_text}
        summary_id = generate_uuid()
        summary = _task_summary_from_result(
            success=success,
            result_text=result_text,
            assignment=payload.get("assignment", {}),
        )
        self._platform.task_summary_index.add(
            IndexedTaskSummary(
                summary_id=summary_id,
                run_id=self._platform.run_id,
                node_id=self.system_id,
                summary=summary,
                scope=SearchScope.DIRECT_CHILD_SUMMARIES,
            )
        )
        node = self._platform.nodes[self.system_id]
        node.summary_refs.append(summary_id)
        await self._eventlog.append(
            "summary_generated",
            {
                "summary_id": summary_id,
                "node_id": self.system_id,
                "goal_achieved": summary.goal_achieved,
                "approach": summary.approach,
            },
            node_id=self.system_id,
            actor_id=self.system_id,
        )
        await self._eventlog.append(
            "s1_completion",
            {
                "s1_id": self.system_id,
                "work_item_id": work_item_id,
                "result": result_payload,
            },
        )

        # REQ 7.8: S3_Allocator へ S1_S3 で完了報告を送信。S3 が未生成 /
        # 未 subscribe の場合は Bus が ``channel_rejected`` を append する。
        s3_id = self._first_system_id(SystemRole.S3_ALLOCATOR)
        if s3_id is not None:
            await self._bus.send(
                Message(
                    message_id=generate_uuid(),
                    sender_role=SystemRole.S1_WORKER,
                    sender_id=self.system_id,
                    receiver_role=SystemRole.S3_ALLOCATOR,
                    receiver_id=s3_id,
                    channel=ChannelId.S1_S3,
                    payload={
                        "type": "completion",
                        "work_item_id": work_item_id,
                        "result": result_payload,
                    },
                    timestamp_ms=int(self._clock.monotonic() * 1000),
                )
            )

        # REQ 9.1: completion を S3*_Auditor の観測トリガとして通知する
        # (auditor 側が ``asyncio.wait({Timer(30s), completion_signal})``
        # で待っている設計。design.md §S3Star_Auditor 参照)。本メソッドは
        # 同期的にイベントをセットするだけのはずなので await 不要。
        # S3*_Auditor が未実装 / メソッド未実装の場合は no-op として
        # 静かにスキップする。
        for auditor in self._platform.systems.get(SystemRole.S3STAR_AUDITOR, []):
            notify = getattr(auditor, "notify_completion", None)
            if callable(notify):
                try:
                    notify()
                except Exception:
                    # auditor 側の例外で S1 を倒してはいけない (REQ 9.1
                    # は auditor の独立性を要求する)。
                    pass

    # ------------------------------------------------------------------
    # Coordination directive handling (REQ 8.5)
    # ------------------------------------------------------------------

    async def _handle_directive(self, msg: Message) -> None:
        """REQ 8.5: directive を受信したら 1 秒以内に ack を返送する。

        ``MessageBus.send`` は同一 event-loop tick 内で完了するため、
        本メソッド全体が 1 秒以内に完了することは構造的に保証される。
        directive の ``directive`` フィールドの内容に基づく実際の挙動
        変更 (例: 特定の work_item を中止する等) は PoC では Event_Log
        に記録するのみで、S1 のメインループへの強制反映は将来拡張。

        Parameters
        ----------
        msg : Message
            S2_Coordinator から S1_S2 で送信された directive。``payload``
            には ``directive_id`` (str) を含む前提。
        """
        payload: dict[str, Any] = msg.payload if isinstance(msg.payload, dict) else {}
        directive_id = payload.get("directive_id")
        if not isinstance(directive_id, str) or not directive_id:
            # schema は ``directive_id: str (min_length=1)`` を要求するため
            # 不正値を弾いて defensive に sentinel を使う (Bus 側 schema
            # 検証で ``coordination_ack`` が拒否されるリスクを下げる)。
            directive_id = "unknown"

        # REQ 8.5: ack を S1_S2 で返送。受信元の ``sender_id`` を
        # ``receiver_id`` として使う。
        await self._bus.send(
            Message(
                message_id=generate_uuid(),
                sender_role=SystemRole.S1_WORKER,
                sender_id=self.system_id,
                receiver_role=SystemRole.S2_COORDINATOR,
                receiver_id=msg.sender_id,
                channel=ChannelId.S1_S2,
                payload={
                    "type": "ack",
                    "directive_id": directive_id,
                    "s1_id": self.system_id,
                },
                timestamp_ms=int(self._clock.monotonic() * 1000),
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _first_system_id(self, role: SystemRole) -> str | None:
        """Return the first system_id for ``role`` known to the platform.

        lifecycle は MANDATORY_ROLES の各役割に対し 1 インスタンスのみを
        生成するため、通常は唯一の System が返る。未生成 / 構造制約
        違反の経路では ``None`` を返し、呼び出し側に no-op 経路を
        辿らせる。
        """
        instances = self._platform.systems.get(role, [])
        if not instances:
            return None
        return instances[0].system_id


def _task_summary_from_result(
    *,
    success: bool,
    result_text: str,
    assignment: Any,
) -> TaskSummary:
    """S1 応答を決定論的な短い TaskSummary に変換する。"""
    lines = [line.strip() for line in result_text.splitlines() if line.strip()]
    approach = (lines[0] if lines else ("完了" if success else "実行失敗"))[:240]
    questions = tuple(line[:240] for line in lines if line.endswith(("?", "？")))
    preconditions = json.dumps(
        assignment,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )[:400]
    return TaskSummary(
        goal_achieved=success,
        approach=approach,
        preconditions=preconditions,
        dead_ends=() if success else (approach,),
        open_questions=questions,
        reusability_hints=(approach,) if success else (),
    )
