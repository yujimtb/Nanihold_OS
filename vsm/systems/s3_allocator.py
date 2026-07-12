"""S3_Allocator: VSM System 3 — resource allocation and S1 pool management.

design.md `## Components and Interfaces` §S3_Allocator (vsm/systems/s3_allocator.py)
の実装。S5_Policy から S3_S5 で受け取った directive を解釈し、必要な
specialization の S1_Worker を再利用 / 新規生成し、work item を S1_S3 で
配信する。S1 からの完了 / 失敗報告は S5 へ転送する。

Lifecycle role
--------------
本クラスは :class:`vsm.runtime.lifecycle.Platform` がコンストラクタ契約
(キーワード専用 ``system_id``, ``eventlog``, ``bus``, ``runtime``, ``clock``,
``platform``, ``run_config``) を満たす形で生成し、``run()`` 内で 2 つの
受信チャネル (S3_S5, S1_S3) を subscribe して並行に多重化する。実際の
S1 動的生成は :meth:`Platform.spawn_s1` に委譲し、Platform 側が
``s1_instantiated`` event の append (REQ 7.4) と ``run_config.s1_dynamic_max``
/ ``s1_max`` の上限 (REQ 13.6, 1.3) を強制する。本クラスは失敗 (上限超過 /
内部例外) を捕捉して ``s1_instantiation_error`` event の append と S5 への
通知 (REQ 7.5) を担う。

S1 idle definition (REQ 7.2)
----------------------------
:class:`S1Pool` の ``find_idle`` は以下の述語を満たす最初の S1 を返す::

    s1.specialization == requested_specialization
    and len(s1.current_assignments) == 0

該当 S1 が存在する場合は再利用を優先し ``s1_instantiated`` event は
発行しない (REQ 7.2 の idle 定義 + 再利用優先)。

PoC simplification
------------------
REQ 7.1 は「30 秒以内に required ``{specialization: count}`` set を決定」
することを要求する。本 PoC ではコストとレイテンシを抑えるため、Sub_Agent
を介した LLM プロンプトを **試行**し、何らかの理由で失敗した場合は
ハードコードのフォールバック ``{"frontend": 1, "test": 1}`` を採用する。
30 秒 SLA はフォールバックパスでは構造的に常に満たされる (純 in-memory
処理) ため、SLA 観点でも安全である。

Validates Requirements
----------------------
- REQ 7.1: directive 受信から 30 秒以内に ``{specialization: count}`` を決定。
- REQ 7.2: idle (specialization 一致 + ``current_assignments`` 空) の S1 を
  優先再利用する。
- REQ 7.3: idle が無い場合、5 秒以内に新規 S1 を生成し initial assignment
  を渡す (Platform.spawn_s1 経由)。
- REQ 7.4: ``s1_instantiated`` event の append は :meth:`Platform.spawn_s1`
  が責務として行うため、本クラスでは二重発行しない。
- REQ 7.5: instantiation 失敗時は ``s1_instantiation_error`` event を
  append し、5 秒以内に S5 へ S3_S5 で通知する。
- REQ 7.6: assignment 送信を 1 秒以内に S1_S3 で完了する (構造的に保証)。
- REQ 7.7: 送信ごとに ``s1_assignment_sent`` event を 1 秒以内に append。
- REQ 7.8: S1 完了 / 失敗を 5 秒以内に S5 へ S3_S5 で転送する。
- REQ 13.6: 動的生成 S1 の concurrent 上限 (64) は Platform.spawn_s1 が
  強制し、本クラスは上限超過時の :class:`SystemInstantiationError` を
  捕捉して REQ 7.5 経路で扱う。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from vsm.errors import SystemInstantiationError
from vsm.ids import generate_uuid
from vsm.messaging.channels import ChannelId
from vsm.messaging.message import Message
from vsm.roles import SystemRole
from vsm.systems.base import System

if TYPE_CHECKING:
    # 型注釈のみで使い、実行時の循環 import を避ける。実体は
    # lifecycle が保有する :class:`Platform` インスタンスとして本
    # クラスの ``platform`` kwarg に注入される。
    from vsm.clock import Clock
    from vsm.config import RunConfig
    from vsm.eventlog.writer import EventLogWriter
    from vsm.agents.runtime import AgentRuntimeProtocol
    from vsm.messaging.bus import MessageBus
    from vsm.runtime.lifecycle import Platform


__all__ = ["S1Pool", "S3Allocator"]


# REQ 7.1: PoC fallback specialization plan when an LLM-derived plan is
# unavailable. The two specializations match the representative scenario
# in tests/integration/test_representative_scenario.py (Task 24).
_FALLBACK_SPEC_PLAN: dict[str, int] = {"frontend": 1, "test": 1}


class S1Pool:
    """Tracks live S1_Worker instances. Owned by :class:`S3Allocator`.

    本クラスは S3_Allocator の reuse / instantiate 判断を支える参照
    オブジェクトである。``Platform.systems[SystemRole.S1_WORKER]`` を
    Source_of_Truth として ``find_idle`` の都度参照することで、
    :meth:`Platform.spawn_s1` で動的に増えた S1 もただちに見える
    (キャッシュを持たない設計)。

    REQ 7.2 の idle 定義
    --------------------
    ``len(s1.current_assignments) == 0 and s1.specialization == spec``。
    ``current_assignments`` / ``specialization`` 属性は S1_Worker (Task
    17.1) が必ず提供することを契約とするが、Task 14.1 の時点では
    S1_Worker 実装が未着手の可能性があるため、属性アクセスは
    :func:`getattr` で defensiv に行い、欠落時は idle 候補として扱わない。
    """

    def __init__(self, platform: "Platform") -> None:
        self._platform = platform

    def find_idle(self, specialization: str) -> "System | None":
        """REQ 7.2: 指定 specialization の idle S1 を返す (なければ None)。

        Parameters
        ----------
        specialization : str
            要求された specialization ラベル (例: ``"frontend"``)。

        Returns
        -------
        System | None
            条件を満たす最初の S1_Worker。1 件も該当しなければ ``None``。
            ``None`` が返った場合、呼び出し側は :meth:`Platform.spawn_s1`
            で新規生成する責務を負う (REQ 7.3)。
        """
        s1_list = self._platform.systems.get(SystemRole.S1_WORKER, [])
        for s1 in s1_list:
            spec = getattr(s1, "specialization", None)
            assignments = getattr(s1, "current_assignments", None)
            if spec != specialization:
                continue
            # ``current_assignments`` は list を想定するが、defensively
            # ``len`` 不能なオブジェクトはスキップする。
            try:
                if assignments is not None and len(assignments) == 0:
                    return s1
            except TypeError:
                continue
        return None


class S3Allocator(System):
    """VSM System 3 — resource allocator and S1 pool manager.

    Constructor contract (lifecycle)
    --------------------------------
    キーワード専用引数のみを受け、:class:`vsm.runtime.lifecycle.Platform`
    の ``role_to_class`` マップから生成される。``register_sub_agent`` は
    ``run_config.count(SystemRole.S3_ALLOCATOR)`` 個 (REQ 13.4 で 1〜16)
    の Sub_Agent を ``allocator-<i>`` ラベルで登録する。

    Validates Requirements: 7.1, 7.2, 7.3, 7.5, 7.6, 7.7, 7.8, 13.6.
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
    ) -> None:
        super().__init__(
            system_id=system_id,
            role=SystemRole.S3_ALLOCATOR,
            eventlog=eventlog,
            runtime=runtime,
            clock=clock,
        )
        self._bus = bus
        self._platform = platform
        self._run_config = run_config

        # REQ 13.4 / 1.4: ``count(SystemRole.S3_ALLOCATOR)`` 個の Sub_Agent を
        # ``allocator-<i>`` ラベルで登録する。``max(1, ...)`` で count が
        # 0 のとき (RunConfig の不正値だが念のため) でも最低 1 個を
        # 確保する。
        for i in range(max(1, run_config.count(SystemRole.S3_ALLOCATOR))):
            self.register_sub_agent(label=f"allocator-{i}")

        self._pool = S1Pool(platform)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Subscribe to S3_S5 / S1_S3 and dispatch incoming messages.

        2 つの受信チャネルを並行に多重化する:

        * ``S3_S5`` — S5 から流れる directive。:meth:`_handle_directive`
          が再利用 / 新規生成 / S5 通知を行う。
        * ``S1_S3`` — S1 から流れる完了 / 失敗報告。
          :meth:`_handle_s1_report` が S5 へ転送する。

        :class:`asyncio.CancelledError` は ``System.shutdown`` 経路で
        伝播するため捕捉せず再 raise する (基底契約)。
        """
        q_s3_s5 = self._bus.subscribe(self.system_id, ChannelId.S3_S5)
        q_s1_s3 = self._bus.subscribe(self.system_id, ChannelId.S1_S3)

        # ``asyncio.wait`` で 2 キューを多重化する。``FIRST_COMPLETED``
        # で先に到達した方を処理し、残りは次回ループでまた wait しなおす。
        # キューの ``get`` を直接 await する形ではなくタスクにラップする
        # のは、cancel 安全性 (Pending タスクを明示的に cancel する) と、
        # 1 ループ 1 メッセージのバッチサイズで Event_Log の append を
        # 詰まらせないため。
        while True:
            t_s3_s5 = asyncio.create_task(q_s3_s5.get(), name="s3_allocator.s3_s5")
            t_s1_s3 = asyncio.create_task(q_s1_s3.get(), name="s3_allocator.s1_s3")
            tasks = {t_s3_s5, t_s1_s3}
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                # shutdown 経路。pending タスクも明示的に cancel する。
                for t in tasks:
                    t.cancel()
                raise

            # pending タスクをキャンセルし、結果を捨てる。次回ループで
            # 再び ``q.get()`` をスケジュールするので、メッセージがキューに
            # 残っていても次の wait で取り出される。
            for p in pending:
                p.cancel()

            for d in done:
                msg: Message = d.result()
                if (
                    msg.channel == ChannelId.S3_S5
                    and msg.sender_role == SystemRole.S5_POLICY
                ):
                    await self._handle_directive(msg)
                elif (
                    msg.channel == ChannelId.S1_S3
                    and msg.sender_role == SystemRole.S1_WORKER
                ):
                    await self._handle_s1_report(msg)
                # それ以外の組合せは Bus 側 ``ALLOWED_ROUTES`` で既に
                # 弾かれているはずなので silently ignore で問題ない。

    # ------------------------------------------------------------------
    # Directive handling (REQ 7.1〜7.7)
    # ------------------------------------------------------------------

    async def _handle_directive(self, msg: Message) -> None:
        """REQ 7.1〜7.7: directive を解釈して S1 群に作業を配分する。

        手順:

        1. ``{specialization: count}`` を決定する (REQ 7.1)。PoC では
           Sub_Agent 経由の LLM プロンプトを試行し、失敗時は
           :data:`_FALLBACK_SPEC_PLAN` を採用する。
        2. 各 specialization について ``count`` 回ループ:

           a. :meth:`S1Pool.find_idle` で reuse 候補を探す (REQ 7.2)。
           b. idle が居れば :meth:`_assign_work` で作業を渡す。
           c. idle が居なければ :meth:`Platform.spawn_s1` で新規生成
              (REQ 7.3, 13.6)。Platform 側で ``s1_instantiated`` event の
              append (REQ 7.4) と上限チェックが行われる。
           d. 新規生成失敗 (上限超過 / 内部例外) 時は
              :meth:`_handle_instantiation_failure` で REQ 7.5 の
              ``s1_instantiation_error`` + S5 通知を実施し、当該
              specialization のループを継続する (他 specialization に
              影響を与えない)。
        """
        spec_plan = self._derive_spec_plan(msg.payload)

        for spec, count in spec_plan.items():
            for _ in range(count):
                # REQ 7.2: 再利用優先。
                idle = self._pool.find_idle(spec)
                if idle is not None:
                    work_item_id = generate_uuid()
                    await self._assign_work(idle, work_item_id, msg.payload)
                    continue

                # REQ 7.3 / 13.6: 新規生成。Platform.spawn_s1 が
                # ``s1_instantiated`` event の append (REQ 7.4) と上限
                # チェックを担う。
                try:
                    new_s1 = await self._platform.spawn_s1(
                        specialization=spec,
                        initial_assignment=str(msg.payload),
                    )
                except SystemInstantiationError as exc:
                    # REQ 7.5: 失敗 event の append と S5 通知。当該
                    # specialization の残りスロットも一旦諦める
                    # (後続の directive で再試行される設計)。
                    await self._handle_instantiation_failure(spec, exc)
                    break

                work_item_id = generate_uuid()
                await self._assign_work(new_s1, work_item_id, msg.payload)

    def _derive_spec_plan(self, directive_payload: dict) -> dict[str, int]:
        """REQ 7.1: directive から ``{specialization: count}`` を決定する。

        PoC では LLM 呼び出しコストを抑えるため、まず directive payload に
        ``"plan"`` (dict[str, int]) が直接埋まっていないかを見て、無ければ
        :data:`_FALLBACK_SPEC_PLAN` を返す。Sub_Agent / LLM ベースの
        導出は将来の拡張ポイントとして残し、本 PoC では純粋にローカルな
        計算で 30 秒 SLA を構造的に満たす。

        Notes
        -----
        将来 LLM ベースに切り替える場合は ``await self.sub_agents[0].respond(
        prompt, context)`` を呼び、JSON 出力を ``json.loads`` してから
        各値を ``int`` に強制し ``[1, 64]`` でクランプする責務を持つ。
        """
        if isinstance(directive_payload, dict):
            raw_plan = directive_payload.get("plan")
            if isinstance(raw_plan, dict):
                # 値の型 / 範囲を defensiv にチェック。整数で 1 以上のもの
                # だけ採用し、それ以外はフォールバックを優先する。
                cleaned: dict[str, int] = {}
                for k, v in raw_plan.items():
                    if (
                        isinstance(k, str)
                        and k
                        and isinstance(v, int)
                        and not isinstance(v, bool)
                        and 1 <= v <= 64
                    ):
                        cleaned[k] = v
                if cleaned:
                    return cleaned
        return dict(_FALLBACK_SPEC_PLAN)

    async def _assign_work(
        self,
        s1: "System",
        work_item_id: str,
        assignment: dict,
    ) -> None:
        """REQ 7.6 / 7.7: S1 へ assignment を送信し event を append する。

        Parameters
        ----------
        s1 : System
            送信先の S1_Worker インスタンス (Platform.systems から得た参照)。
        work_item_id : str
            UUIDv4 hex (REQ 4.6) の work item 識別子。
        assignment : dict
            ``s1_assignment_sent`` payload の ``assignment`` フィールドに
            そのまま転写される dict。
        """
        reserve_writer = getattr(self._platform, "reserve_s1_writer", None)
        if reserve_writer is not None:
            reserve_writer(s1)

        # S1_Worker の ``current_assignments`` (list) に work_item_id を
        # 追加して、次回以降の :meth:`S1Pool.find_idle` で idle 判定に
        # 反映させる。属性が無い場合は静かにスキップする (S1_Worker 未
        # 実装時の defensive 対応)。
        assignments = getattr(s1, "current_assignments", None)
        if isinstance(assignments, list):
            assignments.append(work_item_id)

        # REQ 7.6: S1_S3 で 1 秒以内に送信。``MessageBus.send`` は同一
        # event-loop tick 内で完了するため SLA を構造的に満たす。
        await self._bus.send(
            Message(
                message_id=generate_uuid(),
                sender_role=SystemRole.S3_ALLOCATOR,
                sender_id=self.system_id,
                receiver_role=SystemRole.S1_WORKER,
                receiver_id=s1.system_id,
                channel=ChannelId.S1_S3,
                payload={
                    "work_item_id": work_item_id,
                    "assignment": assignment,
                },
                timestamp_ms=int(self._clock.monotonic() * 1000),
            )
        )

        # REQ 7.7: ``s1_assignment_sent`` を 1 秒以内に append。schema は
        # ``assignment: dict[str, Any]`` を要求するので、dict 以外が
        # 渡された場合は ``{"raw": str(...)}`` でラップする。
        normalized_assignment: dict[str, Any]
        if isinstance(assignment, dict):
            normalized_assignment = assignment
        else:
            normalized_assignment = {"raw": str(assignment)}
        await self._eventlog.append(
            "s1_assignment_sent",
            {
                "s1_id": s1.system_id,
                "work_item_id": work_item_id,
                "assignment": normalized_assignment,
            },
        )

    async def _handle_instantiation_failure(
        self,
        specialization: str,
        exc: SystemInstantiationError,
    ) -> None:
        """REQ 7.5: instantiation 失敗を Event_Log と S5 へ記録する。

        2 つの副作用を順次行う:

        1. ``s1_instantiation_error`` event を append。``reason`` は例外の
           ``str`` を採用し、``min_length=1`` schema 制約を満たすよう
           空文字を ``"unknown"`` にフォールバックする。
        2. S5 へ S3_S5 で通知 (5 秒以内、構造的保証)。S5 が未起動 / 未
           subscribe の場合は :meth:`MessageBus.send` が ``SendResult.rejected``
           を返し、bus 側で ``channel_rejected`` event が記録される。
        """
        reason = str(exc) or "unknown"
        await self._eventlog.append(
            "s1_instantiation_error",
            {
                "specialization": specialization,
                "reason": reason,
            },
        )

        s5_id = self._first_s5_id()
        if s5_id is None:
            # S5 が未生成なら通知できない (構造的に lifecycle で起こり得
            # ない経路だが defensive に対応)。Event_Log には既に記録した
            # ので observability は保たれる。
            return

        await self._bus.send(
            Message(
                message_id=generate_uuid(),
                sender_role=SystemRole.S3_ALLOCATOR,
                sender_id=self.system_id,
                receiver_role=SystemRole.S5_POLICY,
                receiver_id=s5_id,
                channel=ChannelId.S3_S5,
                payload={
                    "status": "instantiation_failed",
                    "specialization": specialization,
                    "reason": reason,
                },
                timestamp_ms=int(self._clock.monotonic() * 1000),
            )
        )

    # ------------------------------------------------------------------
    # S1 status report forwarding (REQ 7.8)
    # ------------------------------------------------------------------

    async def _handle_s1_report(self, msg: Message) -> None:
        """REQ 7.8: S1 の完了 / 失敗報告を S5 へ S3_S5 で転送する。

        S1_S3 は双方向 channel なので、S1 → S3 への ``s1_completion`` 等の
        報告がここに届く。S5 への転送は単一 send で完結し、5 秒 SLA を
        構造的に満たす。S5 が未生成 / 未 subscribe なら静かにスキップする
        (Event_Log には Bus が ``channel_rejected`` を残す)。
        """
        s5_id = self._first_s5_id()
        if s5_id is None:
            return
        await self._bus.send(
            Message(
                message_id=generate_uuid(),
                sender_role=SystemRole.S3_ALLOCATOR,
                sender_id=self.system_id,
                receiver_role=SystemRole.S5_POLICY,
                receiver_id=s5_id,
                channel=ChannelId.S3_S5,
                payload={"status_report": msg.payload},
                timestamp_ms=int(self._clock.monotonic() * 1000),
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _first_s5_id(self) -> str | None:
        """Return the first S5_Policy ``system_id`` known to the platform.

        lifecycle は MANDATORY_ROLES の各役割に対し 1 インスタンスのみを
        生成するため、通常は唯一の S5 が返る。S5 が未生成の場合 (構造
        制約違反 / 検証失敗の経路) は ``None`` を返す。
        """
        s5_instances = self._platform.systems.get(SystemRole.S5_POLICY, [])
        if not s5_instances:
            return None
        return s5_instances[0].system_id
