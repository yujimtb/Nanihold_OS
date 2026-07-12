"""S5_Policy: 政策決定と並行ディスパッチを担う System (Task 12.1).

design.md `## Components and Interfaces` §S5_Policy と
``## Data Models`` §Event スキーマ (``policy_decision`` /
``dispatch_error``) に対応する具象 :class:`System`。本モジュールは
Run の "判断中枢" として、S4_Scanner から到着した
``EnvironmentAssessment`` を Sub_Agent (LLM 経由) に解釈させ、
S3_Allocator 向けの directive と S4_Scanner 向けの follow-up 調査
依頼を **並行発行** することで VSM における S3 と S4 のバランスを
取る (REQ 6.1〜6.6)。

実行モデル
----------
``run()`` は 3 つの受信チャネルを subscribe して
:func:`asyncio.wait` (``FIRST_COMPLETED``) で待つ:

* :data:`~vsm.messaging.channels.ChannelId.S4_S5` —
  S4_Scanner からの ``EnvironmentAssessment``。届いた瞬間に
  :meth:`_handle_assessment` で PolicyDecision を生成し、両側へ
  並行ディスパッチする (本 System の主機能)。
* :data:`~vsm.messaging.channels.ChannelId.S3_S5` —
  S3_Allocator からの内部 status report (REQ 7.8 送信側)。MVP では
  受信のみで観測アクションは無いが、購読していないと Bus が
  ``channel_rejected`` を返してしまうので必ず subscribe する。
* :data:`~vsm.messaging.channels.ChannelId.S3STAR_S5_AUDIT` —
  S3*_Auditor からの audit finding (REQ 9.5 受領側)。MVP では
  ログのみだが、購読責務は本 System にある。

並行ディスパッチ (REQ 6.2 / 6.3 / 6.4 / 6.5)
-------------------------------------------
PolicyDecision 生成直後、:func:`asyncio.gather` を
``return_exceptions=True`` で起動して S3 / S4 へ同時に送る:

* 個々の :meth:`MessageBus.send` は in-memory の
  ``Queue.put_nowait`` + Event_Log append のみで構成されるため
  500 ms SLA (REQ 6.2 / 6.3) を構造的に満たす。
* 並行発行 (gather) により両側合計が 1 秒 SLA (REQ 6.4) に収まる
  (逐次 500 + 500 = 1000 ms ぎりぎりを回避する設計)。
* ``return_exceptions=True`` により片側で例外が発生してももう片方の
  Future はキャンセルされない (REQ 6.5: 片側失敗が他方をブロック
  しない構造保証)。
* :class:`SendResult` は非例外で ``delivered=False`` を返しうるので、
  その経路でも ``dispatch_error`` を append する (REQ 6.5)。

Validates Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 9.5.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from vsm.clock import Clock
from vsm.config import RunConfig
from vsm.eventlog.writer import EventLogWriter
from vsm.ids import generate_uuid
from vsm.agents.runtime import AgentRuntimeProtocol
from vsm.errors import QuotaExhaustedError
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId
from vsm.messaging.message import Message, SendResult
from vsm.roles import SystemRole
from vsm.systems.base import System
from vsm.systems.prompts import (
    build_s5_algedonic_prompt,
    build_s5_policy_prompt,
)

if TYPE_CHECKING:
    # 循環参照を避けるため Platform は型注釈のみで参照する。
    # 実体 import は :class:`Platform` のインスタンスを ``__init__`` で
    # 受け取った後にだけ必要 (役割別 system_id 解決用)。
    from vsm.runtime.lifecycle import Platform


__all__ = ["S5Policy"]


class S5Policy(System):
    """VSM System 5 — 政策決定と S3/S4 への並行ディスパッチを担う System.

    Lifecycle
    ---------
    1. :meth:`Platform.create` から kwarg-only コンストラクタで
       インスタンス化される。``run_config.count(SystemRole.S5_POLICY)``
       個の Sub_Agent (1〜16, REQ 13.4) を ``__init__`` 中に登録する。
    2. :meth:`Platform.start` 経由で ``run()`` が asyncio Task として
       起動する。受信ループは ``shutdown()`` で cancel されるまで継続。
    3. ``run()`` は S4_S5 / S3_S5 / S3STAR_S5_AUDIT の 3 チャネルを
       subscribe し、いずれかのキューにメッセージが届くたびに
       適切なハンドラへ dispatch する。

    Parameters
    ----------
    system_id : str
        UUIDv4 (32 文字 hex)。``Platform.create`` が
        :func:`vsm.ids.generate_uuid` で発行する。
    eventlog : EventLogWriter
        Run スコープの単一 writer。``policy_decision`` /
        ``dispatch_error`` の append に使う。
    bus : MessageBus
        Run スコープの単一 :class:`MessageBus`。本 System は
        ``subscribe`` と ``send`` 双方を呼ぶ。
    runtime : AgentRuntimeProtocol | None
        Sub_Agent が呼び出すロール別 AgentRuntime。
    clock : Clock
        SLA 計測 / メッセージ ``timestamp_ms`` の生成に使うクロック。
    platform : Platform
        役割別 system_id 解決のための後方参照。具体的には
        S3_Allocator の ``system_id`` を ``platform.systems`` から
        引いて Message の ``receiver_id`` に充てる。
    run_config : RunConfig
        Run 構造設定。Sub_Agent 数決定にのみ使用する。

    Validates Requirements: 1.1, 1.4, 6.1〜6.6, 9.5.
    """

    def __init__(
        self,
        *,
        system_id: str,
        eventlog: EventLogWriter,
        bus: MessageBus,
        runtime: AgentRuntimeProtocol | None,
        clock: Clock,
        platform: "Platform",
        run_config: RunConfig,
    ) -> None:
        super().__init__(
            system_id=system_id,
            role=SystemRole.S5_POLICY,
            eventlog=eventlog,
            runtime=runtime,
            clock=clock,
        )
        self._bus = bus
        self._platform = platform
        self._run_config = run_config

        # REQ 1.4 / 13.4: ``run_config.count(S5_POLICY)`` は構築時に
        # ``[1, 16]`` の範囲で検証済 (vsm.config._validate_sub_agent_count)
        # のため、ここでは確実に 1 個以上を登録できる。
        # ``max(1, ...)`` は defensive: RunConfig 未経由で直接呼ばれた
        # 場合 (テストで ``sub_agents_per_role`` を空にしたケースなど)
        # でも 1 個の Sub_Agent を保証する (基底 System.run_config は
        # MVP では役割固有のラベル割当を持たないため、すべて
        # ``policy-{i}`` で番号付けする)。
        count = max(1, run_config.count(SystemRole.S5_POLICY))
        for i in range(count):
            self.register_sub_agent(label=f"policy-{i}")

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """3 チャネルを購読してメッセージを処理するメインループ.

        Validates Requirements
        ----------------------
        - REQ 6.1: S4_Scanner からの ``EnvironmentAssessment`` 受信時に
          PolicyDecision を生成する分岐を提供する。
        - REQ 9.5 (受信側): S3*_Auditor からの audit finding を
          ``S3STAR_S5_AUDIT`` チャネルで受け取る。MVP では追加の
          応答アクションは無いが、購読は構造的責務として行う。

        ループは ``asyncio.CancelledError`` を捕捉せずそのまま伝搬
        させ、:meth:`Platform.shutdown` 経由の cancel に従って正常
        終了する (基底 :meth:`System.shutdown` の規約)。
        """
        # 3 チャネルを購読する。``subscribe`` は同 ``(receiver_id,
        # channel)`` への 2 重呼び出しに対しても同一キューを返すので、
        # 実際には他コンポーネント (今回は無いが将来的に) と共有
        # しても安全。
        q_s4 = self._bus.subscribe(self.system_id, ChannelId.S4_S5)
        q_s3 = self._bus.subscribe(self.system_id, ChannelId.S3_S5)
        q_audit = self._bus.subscribe(
            self.system_id, ChannelId.S3STAR_S5_AUDIT
        )
        q_algedonic = self._bus.subscribe(self.system_id, ChannelId.ALGEDONIC)

        # ``asyncio.wait(..., FIRST_COMPLETED)`` で 3 つの ``Queue.get``
        # を同時待機する。1 件届いたら他の Future を cancel して次の
        # iteration で作り直す (Queue.get は cancel-safe)。
        while True:
            tasks: set[asyncio.Task[Message]] = {
                asyncio.create_task(q_s4.get(), name="s5_recv_s4_s5"),
                asyncio.create_task(q_s3.get(), name="s5_recv_s3_s5"),
                asyncio.create_task(q_audit.get(), name="s5_recv_audit"),
                asyncio.create_task(q_algedonic.get(), name="s5_recv_algedonic"),
            }
            try:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                # ``shutdown()`` 経由の cancel: pending タスクを片付けて
                # から再 raise する (リソースリーク防止)。
                for t in tasks:
                    t.cancel()
                raise

            for t in pending:
                t.cancel()

            for d in done:
                msg: Message = d.result()
                if msg.channel == ChannelId.S4_S5:
                    # REQ 6.1: S4_Scanner からの assessment 受信のみが
                    # PolicyDecision 生成のトリガ。S5 自身が S4 へ送る
                    # follow-up は ``sender_role == S5_POLICY`` なので
                    # ここでは弾き、誤って自分自身の follow-up を再
                    # 解釈するループを防ぐ。
                    if msg.sender_role == SystemRole.S4_SCANNER:
                        await self._handle_assessment(msg)
                elif msg.channel == ChannelId.S3_S5:
                    # REQ 7.8 送信側 (S3 → S5 status report)。MVP では
                    # 観測 (Event_Log の channel_message として既に
                    # 記録済み) のみで追加アクション無し。
                    pass
                elif msg.channel == ChannelId.S3STAR_S5_AUDIT:
                    # REQ 9.5 受領側 (S3* → S5 audit finding)。MVP では
                    # 観測のみ。後続 task で finding 内容を反映した
                    # PolicyDecision 生成へ拡張する余地を残す。
                    pass
                elif msg.channel == ChannelId.ALGEDONIC:
                    await self._handle_algedonic(msg)

    async def _handle_algedonic(self, msg: Message) -> None:
        payload = msg.payload
        severity = payload.get("severity")
        reason = payload.get("reason")
        source_node_id = payload.get("source_node_id")
        if severity not in {"pain", "pleasure"}:
            raise ValueError("algedonic severity must be pain or pleasure")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("algedonic reason is required")
        if not isinstance(source_node_id, str) or not source_node_id.strip():
            raise ValueError("algedonic source_node_id is required")

        prompt = build_s5_algedonic_prompt(
            severity=severity,
            reason=reason,
            source_node_id=source_node_id,
        )
        response = await self._sub_agents[0].respond(prompt)
        try:
            raw = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise ValueError("S5 algedonic response must be valid JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError("S5 algedonic response must be a JSON object")
        action = raw.get("action")
        action_reason = raw.get("reason")
        if action not in {"suspend", "consortium", "escalate"}:
            raise ValueError("S5 algedonic action must be suspend, consortium, or escalate")
        if not isinstance(action_reason, str) or not action_reason.strip():
            raise ValueError("S5 algedonic response requires reason")

        await self._eventlog.append(
            "algedonic_handled",
            {
                "severity": severity,
                "source_node_id": source_node_id,
                "action": action,
                "reason": action_reason,
            },
            node_id=self.system_id,
            actor_type="agent",
            actor_id=self._sub_agents[0].sub_agent_id,
        )
        if action == "suspend":
            await self._platform.suspend_node_from_algedonic(
                source_node_id=source_node_id,
                reason=action_reason,
                requested_by=self.system_id,
            )
        elif action == "consortium":
            await self._platform.convene_consortium(
                subject=f"Algedonic signal: {reason}",
                convener_node_id=self.system_id,
                trigger="algedonic",
            )
        else:
            await self._eventlog.append(
                "escalation_requested",
                {
                    "reason": action_reason,
                    "blocking_issue": reason,
                    "requested_by": self.system_id,
                    "target_authority": "human",
                },
                node_id=self.system_id,
                actor_id=self.system_id,
            )

    async def convene_consortium(
        self, *, subject: str, participant_node_ids: tuple[str, ...] | None = None
    ):
        """S5 の任意招集トリガ。"""

        return await self._platform.convene_consortium(
            subject=subject,
            convener_node_id=self.system_id,
            participant_node_ids=participant_node_ids,
            trigger="s5",
        )

    # ------------------------------------------------------------------
    # Assessment handler
    # ------------------------------------------------------------------

    async def _handle_assessment(self, msg: Message) -> None:
        """S4_Scanner からの ``EnvironmentAssessment`` を処理する.

        実行順序 (REQ 6.1 / 6.2 / 6.3 / 6.4 / 6.5 / 6.6):

        1. Sub_Agent 経由で LLM を呼び出し、directive と
           follow-up_request を生成する。LLM 例外時は安全な
           fallback 文字列に降格する (Event_Log は ``llm_error`` /
           ``llm_timeout`` を Sub_Agent.respond 側で記録済)。
        2. ``policy_decision`` を Event_Log に append する (REQ 6.6)。
           append 自体は in-memory queue への enqueue なので
           1 秒 SLA は構造的に満たす。
        3. :func:`asyncio.gather` で S3 / S4 への送信を並行発行
           (REQ 6.2 / 6.3 / 6.4)。``return_exceptions=True`` により
           片側失敗が他方をブロックしない (REQ 6.5)。
        4. 各送信結果について、例外 / ``SendResult.rejected`` 双方を
           ``dispatch_error`` として append する (REQ 6.5)。

        Parameters
        ----------
        msg : Message
            S4_Scanner が ``S4_S5`` チャネルで送ってきた assessment。
            ``payload`` は最低限 ``"assessment_id"`` キーを含むことを
            想定する (S4_Scanner Task 13.1 の責務)。

        Validates Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6.
        """
        # ------------------------------------------------------------------
        # Step 1: Sub_Agent で PolicyDecision を生成する
        # ------------------------------------------------------------------
        assessment_payload = msg.payload
        # ``PolicyDecisionPayload.assessment_id`` は min_length=1 を要求
        # するため、欠落時は ``"unknown"`` で代替する (replay 時に
        # source assessment を引けない異常ケースであることが分かる)。
        assessment_id = assessment_payload.get("assessment_id") or "unknown"

        sub_agent = self._sub_agents[0]
        prompt = build_s5_policy_prompt(assessment=assessment_payload)
        try:
            response = await sub_agent.respond(
                prompt, context={"pending_message": msg}
            )
            # PoC では応答全体を directive として採用し、follow-up は
            # 固定文言とする。schema は ``directive`` に min_length=1
            # を要求するため、応答が空文字列なら fallback を使う。
            directive_text = response.text or "execute"
            followup_text = "monitor for changes"
        except QuotaExhaustedError:
            # assessment は QuotaMonitor が保留済みで、復帰後に再配送される。
            return
        except Exception:
            # ``Sub_Agent.respond`` 内で ``llm_timeout`` / ``llm_error``
            # は既に append 済み。ここでは run loop を停止させずに
            # PolicyDecision の発行を続行することを優先し、安全な
            # fallback 文字列で続行する (REQ 6.1 の "directive content"
            # は最低限の文字列で満たされる)。
            directive_text = "fallback directive"
            followup_text = "fallback follow-up"

        decision_id = generate_uuid()

        # ------------------------------------------------------------------
        # Step 2: REQ 6.6 — ``policy_decision`` を append する (1 秒以内)
        # ------------------------------------------------------------------
        # ``EventLogWriter.append`` は in-memory queue への enqueue で
        # 完了するため、SLA 1 秒は構造的に満たされる。
        await self._eventlog.append(
            "policy_decision",
            {
                "decision_id": decision_id,
                "assessment_id": assessment_id,
                "directive": directive_text,
                "followup_request": followup_text,
            },
        )

        # ------------------------------------------------------------------
        # Step 3: REQ 6.2 / 6.3 / 6.4 — S3 / S4 へ並行ディスパッチ
        # ------------------------------------------------------------------
        # メッセージ envelope の ``timestamp_ms`` は wall-clock の
        # ミリ秒精度。``Clock.now()`` は timezone-aware datetime を
        # 返すので ``.timestamp() * 1000`` で UNIX 時刻 ms に変換できる。
        now_ms = int(self._clock.now().timestamp() * 1000)

        s3_receiver_id = self._first_id_of_role(SystemRole.S3_ALLOCATOR)
        # S4 への follow-up は assessment を送ってきた当該 S4 インス
        # タンス (msg.sender_id) に返す。複数 S4 が並行する将来の
        # 拡張に備え、role 全体ではなく個別 ID を保持する。
        s4_receiver_id = msg.sender_id

        send_s3 = self._bus.send(
            Message(
                message_id=generate_uuid(),
                sender_role=SystemRole.S5_POLICY,
                sender_id=self.system_id,
                receiver_role=SystemRole.S3_ALLOCATOR,
                receiver_id=s3_receiver_id,
                channel=ChannelId.S3_S5,
                payload={
                    "decision_id": decision_id,
                    "directive": directive_text,
                },
                timestamp_ms=now_ms,
            )
        )
        send_s4 = self._bus.send(
            Message(
                message_id=generate_uuid(),
                sender_role=SystemRole.S5_POLICY,
                sender_id=self.system_id,
                receiver_role=SystemRole.S4_SCANNER,
                receiver_id=s4_receiver_id,
                channel=ChannelId.S4_S5,
                payload={
                    "decision_id": decision_id,
                    "followup_request": followup_text,
                },
                timestamp_ms=now_ms,
            )
        )

        # REQ 6.5: ``return_exceptions=True`` により片側の例外が他方を
        # キャンセルしない (構造的に "片側失敗が他方をブロックしない"
        # を保証する)。
        results = await asyncio.gather(
            send_s3, send_s4, return_exceptions=True
        )

        # ------------------------------------------------------------------
        # Step 4: REQ 6.5 — dispatch_error を 1 秒以内に append する
        # ------------------------------------------------------------------
        recipients = ("S3_ALLOCATOR", "S4_SCANNER")
        channels = (ChannelId.S3_S5, ChannelId.S4_S5)
        for recipient, channel, result in zip(recipients, channels, results):
            if isinstance(result, BaseException):
                # ``DispatchErrorPayload.reason`` は min_length=1 を
                # 要求するため、空文字列の例外メッセージに対する
                # 防御として ``repr(result)`` をフォールバックに使う。
                reason = str(result) or repr(result)
                await self._eventlog.append(
                    "dispatch_error",
                    {
                        "recipient": recipient,
                        "channel": channel.value,
                        "reason": reason,
                    },
                )
            elif isinstance(result, SendResult) and not result.delivered:
                # Bus が非例外で拒否した経路 (route 不在 / subscriber
                # 未登録) も dispatch failure として扱う (REQ 6.5 の
                # "dispatch to either S3 or S4 fails" の文言は失敗の
                # 種別を区別しない)。``rejected_channel`` が既に
                # ``channel_rejected`` Event として記録されている前提で、
                # ここでは S5 視点の責務として ``dispatch_error`` を
                # 独立に追加する。
                rejected = result.rejected_channel.value if (
                    result.rejected_channel is not None
                ) else channel.value
                await self._eventlog.append(
                    "dispatch_error",
                    {
                        "recipient": recipient,
                        "channel": channel.value,
                        "reason": f"channel rejected: {rejected}",
                    },
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _first_id_of_role(self, role: SystemRole) -> str:
        """``platform.systems`` から ``role`` の最初の System の id を返す.

        Run には PoC の MVP モデルとして役割ごとに 1 つの System を
        想定する (REQ 1.2: "at least one"; PoC では "exactly one" を
        既定とする) ため、最初の要素を採用すれば十分。インスタンスが
        登録されていない (= Run 構造検証を経ていない) 異常状態でも
        Bus 側の route check / subscription lookup が
        ``channel_rejected`` を返すため、ここでは defensively
        ``"unknown"`` を返してダウンストリームでの観測ポイントを残す。

        Parameters
        ----------
        role : SystemRole
            検索対象の役割。本 System では ``S3_ALLOCATOR`` のみが
            実用上の引数。

        Returns
        -------
        str
            該当 System の ``system_id``。未登録時は ``"unknown"``。
        """
        instances = self._platform.systems.get(role, [])
        if not instances:
            return "unknown"
        return instances[0].system_id
