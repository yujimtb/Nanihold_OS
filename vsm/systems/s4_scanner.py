"""S4_Scanner: VSM System 4 — environment-facing scanner (Task 13.1).

design.md `## Components and Interfaces` §S4_Scanner に対応する。S4 は
外部環境を 営業 / リサーチ Sub_Agent を通して走査し、機会と脅威を抽出
した :class:`EnvironmentAssessment` を S5_Policy へ届ける役割を担う。
本実装では assessment は dict 形式 (``{assessment_id, opportunities,
threats}``) として表現する — design.md §Data Models §S4 の暫定形に従う。

Lifecycle
---------
1. :meth:`__init__` (REQ 5.1): 営業 + リサーチ Sub_Agent を **Run 開始
   前** に登録する。:class:`Platform` は Run 開始シーケンスの中で全
   System の ``__init__`` を呼んでから ``run()`` を起動するので、
   ``trigger`` で初回 Task が来た時点で 2 つの Sub_Agent が必ず登録
   済みであることが構造的に保証される。
2. :meth:`trigger` (lifecycle/CLI 経由の入口): 初回 Task の dispatch は
   Channel を経由しない (REQ 5.2 が「Task が dispatch された WHEN」と
   述べているが、PoC では initial Task は CLI が ``Platform.submit``
   経由で S4 に渡す形を採る)。``trigger`` は内部 ``_task_queue`` に
   Task dict を put する非同期メソッド。
3. :meth:`run`: ``_task_queue`` と ``S4_S5`` チャネルを並行 await し、
   どちらかから item が来れば :meth:`_produce_and_deliver` を呼ぶ。

Sub_Agent SLA
-------------
営業 / リサーチの 2 つを :func:`asyncio.gather` で並行起動するのではなく、
**順次 ``asyncio.wait_for(..., 30)``** で起動する。これは、片方が
30 秒タイムアウトした場合でも残りの Sub_Agent は別の 30 秒予算で動ける
ようにするため (REQ 5.5: "continue with the remaining Sub_Agents")。
``gather`` で並行起動すると、片方の ``asyncio.wait_for`` が cancel した
タイミングで他方の ``LLMProvider`` 呼び出しも巻き込まれて中断される
ような scheduling 順序が PoC の決定論を損なうリスクがある。本 PoC では
2 つだけなので逐次でも遅延は最大 60 秒以内に収まり、REQ 5.2 の 60 秒
assessment SLA を満たす。

Delivery retry
--------------
S4_S5 配送は ``MessageBus.send`` が ``SendResult.delivered=False`` を
返した場合に失敗とみなす (REQ 5.6)。最大 3 回までリトライし、各失敗ごと
に ``delivery_error`` event を append する。試行間隔は 10 秒以上 — PoC
では :func:`asyncio.sleep` で実装するが、テストでは ``FakeClock`` と
``monkeypatch`` で sleep を加速する想定。

Validates Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from vsm.clock import Clock
from vsm.config import RunConfig
from vsm.errors import LLMError, LLMProviderError, LLMTimeoutError
from vsm.eventlog.writer import EventLogWriter
from vsm.ids import generate_uuid
from vsm.agents.runtime import AgentRuntimeProtocol
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId
from vsm.messaging.message import Message
from vsm.roles import SystemRole
from vsm.systems.base import System, SubAgent

if TYPE_CHECKING:
    # 循環参照回避: ``Platform`` は :mod:`vsm.runtime.lifecycle` で本クラスを
    # import するため、型注釈用のみの import に留める。
    from vsm.runtime.lifecycle import Platform


__all__ = ["S4Scanner"]


# REQ 5.5: 各 Sub_Agent の個別 SLA。30 秒以内に応答が無ければ
# ``sub_agent_error`` を append し、残りの Sub_Agent で続行する。
_SUB_AGENT_TIMEOUT_SECONDS: float = 30.0

# REQ 5.6: S4_S5 配送リトライ間隔の下限 (10 秒) と最大試行回数 (3 回)。
_DELIVERY_RETRY_INTERVAL_SECONDS: float = 10.0
_DELIVERY_MAX_ATTEMPTS: int = 3

# Sub_Agent ラベル定数。REQ 5.1 が要求する 2 つのラベル名は仕様上
# 日本語のまま採用する (design.md §S4_Scanner が「営業」「リサーチ」と
# 表記)。``str.encode("utf-8")`` で JSONL に round-trip 可能。
_LABEL_SALES: str = "営業"
_LABEL_RESEARCH: str = "リサーチ"


class S4Scanner(System):
    """VSM System 4 — environment scanner (Task 13.1).

    Parameters
    ----------
    system_id : str
        Run 内一意な System 識別子 (lifecycle が UUIDv4 で発行)。
    eventlog : EventLogWriter
        Run 全体で共有される Event_Log writer。
    bus : MessageBus
        Channel 経由の送受信を仲介する :class:`MessageBus`。
    runtime : AgentRuntimeProtocol | None
        Sub_Agent が利用するロール別 AgentRuntime。
    clock : Clock
        SLA 計測用クロック。``FakeClock`` 注入でテスト時に決定論化する。
    platform : Platform
        S5_Policy インスタンス参照を引くために保持する。Initial Task
        受信時に「最初の S5 インスタンス」を recipient とするため。
    run_config : RunConfig
        Run 構造設定。本クラスは現状 :attr:`run_config` を直接読まないが、
        Tasks 12〜17 の他 System と同じ kwarg-only シグネチャを揃える
        ために保持する (design.md §Concrete System constructor contract)。

    Validates Requirements: 5.1.
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
            role=SystemRole.S4_SCANNER,
            eventlog=eventlog,
            runtime=runtime,
            clock=clock,
        )
        self._bus: MessageBus = bus
        self._platform: "Platform" = platform
        self._run_config: RunConfig = run_config

        # REQ 5.1: 営業 / リサーチ Sub_Agent を Run 開始前 (``__init__``) に
        # 登録する。lifecycle は ``run()`` 起動より先にコンストラクタを
        # 呼ぶため、``trigger`` で最初の Task を受け取った時点では 2 つの
        # Sub_Agent が必ず存在する。ラベルは UTF-8 で JSONL に round-trip
        # 可能 (``EventLogWriter`` が ``ensure_ascii=False`` 相当の
        # ``model_dump_json`` を使うため)。
        self.register_sub_agent(label=_LABEL_SALES)
        self.register_sub_agent(label=_LABEL_RESEARCH)

        # 初回 Task の入口。CLI / lifecycle が :meth:`trigger` を通じて
        # dict を put し、:meth:`run` ループが drain する。``Queue`` を
        # ``__init__`` 時点で生成しておくことで、``trigger`` が ``run``
        # より先に呼ばれても安全に enqueue できる (asyncio.Queue は
        # binding loop が決まる前でも put_nowait/await put が可能)。
        self._task_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def trigger(self, task: dict[str, Any]) -> None:
        """Inject an initial Task into S4_Scanner.

        Lifecycle / CLI から呼ばれる「初回 Task の dispatch 入口」。
        REQ 5.2 の「Task が dispatch された WHEN」に該当する起点で、
        Channel を経由せずに :class:`Platform` から S4 に直接渡される。

        Parameters
        ----------
        task : dict
            Task の自由形式 payload。``description`` / ``file_paths``
            などの CLI 投入情報を含むことを想定するが、本クラスは
            payload を ``{task_context!r}`` 文字列化して Sub_Agent
            プロンプトに埋め込むだけで、特定キーには依存しない。

        Notes
        -----
        本メソッドは :class:`asyncio.Queue.put` への薄いラッパであり、
        即座に return する。実際の assessment 生成は :meth:`run` ループ
        の :meth:`_produce_and_deliver` 経路で非同期に進む。
        """
        await self._task_queue.put(task)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Drain ``_task_queue`` and ``S4_S5`` queue concurrently.

        ループの構造:

        1. ``S4_S5`` チャネルに subscribe する (S5_Policy からの follow-up
           受信に備える, REQ 5.7)。
        2. ``_task_queue.get()`` と ``s4_s5_queue.get()`` を
           :func:`asyncio.wait` で同時に await する (FIRST_COMPLETED)。
        3. 完了した側を取り出し、未完了側は cancel する (cancel された
           coroutine は後続の iteration で再度 ``asyncio.create_task``
           される)。Queue は :class:`asyncio.Queue` なので cancel しても
           item を消費していなければ次回 get で再取得できる。
        4. 取得した item の型を判別し、:meth:`_produce_and_deliver` を
           適切な ``recipient_id`` で呼ぶ。

        :class:`asyncio.CancelledError` は ``Platform.shutdown`` 経由で
        投げられるので、握りつぶさずに re-raise する (基底 ``System.shutdown``
        が ``cancel()`` 後に await し、CancelledError を捕捉する)。
        """
        # REQ 5.7: S5 からの follow-up は同じ S4_S5 チャネル上を流れる。
        # ``MessageBus.subscribe`` は ``(receiver_id, channel)`` 単位で
        # キューを分離するので、S4 から S5 へ送ったメッセージが自分の
        # キューに戻ってくる心配はない (sender/receiver 分離は Bus 側で
        # 構造的に保証されている)。
        s4_s5_queue: asyncio.Queue[Message] = self._bus.subscribe(
            self.system_id, ChannelId.S4_S5
        )

        try:
            while True:
                # 各 iteration で getter Task を生成し、wait の結果次第で
                # cancel/再生成する。Queue は ``put`` 側が消費するまで
                # item を保持するので、get Task を cancel しても enqueue
                # された item が失われることはない。
                task_getter = asyncio.create_task(
                    self._task_queue.get(), name="s4_task_get"
                )
                channel_getter = asyncio.create_task(
                    s4_s5_queue.get(), name="s4_s5_get"
                )
                done, pending = await asyncio.wait(
                    {task_getter, channel_getter},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # FIRST_COMPLETED で起きた / 残った Task 両方を整理する。
                # cancel した Task は ``Queue.get`` 内で待機状態だったので、
                # 待機中の future が cancel されるだけで item は残る。
                for fut in pending:
                    fut.cancel()
                    # CancelledError をここで握りつぶしておかないと
                    # 「未捕捉例外」警告が出る場合がある。
                    try:
                        await fut
                    except (asyncio.CancelledError, BaseException):
                        # ``BaseException`` まで拾う必要は通常ないが、
                        # ``Queue.get`` は CancelledError 以外を出さない
                        # ため安全。
                        pass

                for fut in done:
                    item = fut.result()
                    if isinstance(item, Message):
                        # REQ 5.7: S5 からの follow-up リクエスト。
                        # payload に ``followup_request`` キーが入る
                        # (``S5_Policy`` 側の送信契約に従う; 暫定で
                        # キーが無ければ assessment_id を文脈にする)。
                        followup = item.payload.get(
                            "followup_request",
                            item.payload.get("assessment_id", ""),
                        )
                        await self._produce_and_deliver(
                            task_context={"followup_request": followup},
                            recipient_id=item.sender_id,
                        )
                    elif isinstance(item, dict):
                        # 初回 Task: lifecycle が登録した最初の S5 インスタンス
                        # を recipient とする。S5 は MANDATORY_ROLES に
                        # 含まれるので :class:`Platform` 起動成功時には
                        # 必ず 1 つ以上が存在する (REQ 1.2 / 13.1)。
                        s5_instances = self._platform.systems.get(
                            SystemRole.S5_POLICY, []
                        )
                        recipient_id = (
                            s5_instances[0].system_id
                            if s5_instances
                            else self.system_id  # 防御: 自送信は Bus が
                            # ``ALLOWED_ROUTES`` で reject するので、構造
                            # 検証が壊れていることが Event_Log に必ず残る。
                        )
                        await self._produce_and_deliver(
                            task_context=item,
                            recipient_id=recipient_id,
                        )
                    # それ以外の型は構造上来ない (Queue の型注釈で
                    # 保証されている)。
        except asyncio.CancelledError:
            # Lifecycle からの shutdown 経路。基底 System.shutdown が
            # 受け止めるので、ここでは握り潰さず再 raise する。
            raise

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _produce_and_deliver(
        self,
        *,
        task_context: dict[str, Any],
        recipient_id: str,
    ) -> None:
        """Invoke Sub_Agents, build the assessment, and deliver to S5.

        REQ 5.2 / 5.3 / 5.4 / 5.5 / 5.6 / 5.7 を 1 経路でカバーする。

        Steps
        -----
        1. 営業 + リサーチ Sub_Agent を **逐次** に
           :func:`asyncio.wait_for(..., 30s)` で呼ぶ (REQ 5.5)。タイムアウト
           / プロバイダーエラー時は ``sub_agent_error`` を append して
           次の Sub_Agent に進む。
        2. 応答テキストを opportunities / threats に振り分ける (営業 →
           opportunities, リサーチ → threats; design.md §S4_Scanner の
           暫定マッピング)。空文字は REQ 5.3 の "description >= 1 char"
           制約を満たさないので除外する。
        3. ``s4_assessment_produced`` を append (REQ 5.2 / 5.3)。
        4. S5 へ S4_S5 で送信。``SendResult.delivered`` が False の場合は
           ``delivery_error`` を append して 10 秒後にリトライ (最大 3 回,
           REQ 5.6)。

        Parameters
        ----------
        task_context : dict
            Initial Task または follow-up リクエストのコンテキスト。
            プロンプトに ``str(task_context)`` で埋め込む。
        recipient_id : str
            assessment 配送先の S5_Policy インスタンス ``system_id``。
        """
        opportunities: list[str] = []
        threats: list[str] = []

        for sub_agent in self.sub_agents:
            # REQ 5.5: 各 Sub_Agent の経過時間は ``monotonic`` で計測し、
            # タイムアウト時の ``elapsed_ms`` 算出に使う。``LLMTimeoutError``
            # は ``SubAgent.respond`` 内部の ``asyncio.wait_for(60)`` で
            # 既に投げられた場合に使うが、本層では 30 秒の "外側"
            # ``asyncio.wait_for`` が先に発火するため通常は
            # ``asyncio.TimeoutError`` を捕捉することになる。
            started = self._clock.monotonic()
            prompt = (
                f"You are the {sub_agent.label} Sub_Agent of S4_Scanner. "
                f"Examine the following input and produce a single concise "
                f"sentence describing one observation:\n{task_context!r}"
            )
            try:
                response = await asyncio.wait_for(
                    sub_agent.respond(prompt=prompt),
                    timeout=_SUB_AGENT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                # REQ 5.5: 30 秒経過。基底 SubAgent.respond は内部の 60 秒
                # ``wait_for`` でも cancel 経路を持つが、本層 30 秒の方が
                # 先に発火する場合は ``asyncio.TimeoutError`` がここに
                # 上がってくる。``elapsed_ms`` は本層の経過時間で計測。
                elapsed = self._clock.monotonic() - started
                await self._eventlog.append(
                    "sub_agent_error",
                    {
                        "sub_agent_id": sub_agent.sub_agent_id,
                        "elapsed_ms": max(0, int(elapsed * 1000)),
                        "reason": (
                            f"sub_agent timed out after "
                            f"{_SUB_AGENT_TIMEOUT_SECONDS:.0f}s "
                            "(REQ 5.5)"
                        ),
                    },
                )
                # REQ 5.5: 残りの Sub_Agent で続行する。
                continue
            except (LLMTimeoutError, LLMProviderError, LLMError) as exc:
                # 60 秒タイムアウト / プロバイダーエラーは ``SubAgent.respond``
                # 側で既に ``llm_timeout`` / ``llm_error`` を append 済み
                # だが、S4 観点でも ``sub_agent_error`` を残しておくこと
                # で REQ 5.5 のトレースを完成させる。
                elapsed = self._clock.monotonic() - started
                await self._eventlog.append(
                    "sub_agent_error",
                    {
                        "sub_agent_id": sub_agent.sub_agent_id,
                        "elapsed_ms": max(0, int(elapsed * 1000)),
                        "reason": f"{type(exc).__name__}: {exc}",
                    },
                )
                continue

            # REQ 5.3: description は 1 文字以上であること。LLM が空文字
            # を返した場合 (``FakeLLMProvider`` で意図的に設定された等)
            # は assessment item に含めない。
            text = (response.text or "").strip()
            if not text:
                continue
            if sub_agent.label == _LABEL_SALES:
                opportunities.append(text)
            else:
                threats.append(text)

        # REQ 5.2 / 5.3: assessment を組み立てて Event_Log に記録する。
        # ``assessment_id`` は UUIDv4 hex (32 chars) で REQ 4.6 と同様に
        # ``min_length=1`` を満たす。``opportunities`` / ``threats`` は
        # ここまでの filter で各要素 length >= 1 が保証されている。
        assessment_id = generate_uuid()
        await self._eventlog.append(
            "s4_assessment_produced",
            {
                "assessment_id": assessment_id,
                "opportunities": opportunities,
                "threats": threats,
            },
        )

        # REQ 5.4 / 5.6: S5 へ配送 (3 回まで 10 秒間隔リトライ)。
        await self._deliver_to_s5(
            assessment_id=assessment_id,
            opportunities=opportunities,
            threats=threats,
            recipient_id=recipient_id,
        )

    async def _deliver_to_s5(
        self,
        *,
        assessment_id: str,
        opportunities: list[str],
        threats: list[str],
        recipient_id: str,
    ) -> None:
        """Send the assessment to S5_Policy with up to 3 retries.

        REQ 5.6 の本体。``MessageBus.send`` は disallowed route や
        未 subscribe 受信者の場合に :meth:`SendResult.rejected` を返す
        ため、``delivered`` フラグを毎回確認する。例外 (Bus 内部の
        ``OSError`` 等) も同様に試行失敗として扱い、``delivery_error``
        を append する。

        試行間隔は ``asyncio.sleep(_DELIVERY_RETRY_INTERVAL_SECONDS)``
        で確保する — テストでは ``monkeypatch.setattr("asyncio.sleep",
        ...)`` または ``FakeClock`` ベースのアダプタで加速する想定。
        """
        payload: dict[str, Any] = {
            "assessment_id": assessment_id,
            "opportunities": opportunities,
            "threats": threats,
        }

        for attempt in range(1, _DELIVERY_MAX_ATTEMPTS + 1):
            # ``timestamp_ms`` は Bus 側でも別途 Event_Log の ``ts`` が
            # 付くが、Message 自体にも millisecond timestamp を持たせる
            # (REQ 2.9 のための envelope フィールド)。``time_ns`` ベース
            # の wall clock ではなく、注入クロックの ``now()`` を使う。
            msg = Message(
                message_id=generate_uuid(),
                sender_role=SystemRole.S4_SCANNER,
                sender_id=self.system_id,
                receiver_role=SystemRole.S5_POLICY,
                receiver_id=recipient_id,
                channel=ChannelId.S4_S5,
                payload=payload,
                timestamp_ms=int(
                    self._clock.now().timestamp() * 1000
                ),
            )

            try:
                result = await self._bus.send(msg)
            except Exception as exc:  # noqa: BLE001 — Bus 内部の I/O 等
                # REQ 5.6: 失敗ごとに 1 件 ``delivery_error`` を残す。
                await self._eventlog.append(
                    "delivery_error",
                    {
                        "attempt": attempt,
                        "channel": ChannelId.S4_S5.value,
                        "reason": f"{type(exc).__name__}: {exc}",
                    },
                )
            else:
                if result.delivered:
                    # 成功: REQ 5.4 の 5 秒 SLA は Bus が同 tick で put_nowait
                    # するため構造的に満たされる。リトライループ脱出。
                    return
                # ``rejected_channel`` は disallowed route または未 subscribe
                # 受信者を意味する。``MessageBus.send`` は同時に
                # ``channel_rejected`` も append している (REQ 2.7 / 2.8)
                # が、REQ 5.6 の観点で S4 側にも 1 件残す。
                rejected = (
                    result.rejected_channel.value
                    if result.rejected_channel is not None
                    else ChannelId.S4_S5.value
                )
                await self._eventlog.append(
                    "delivery_error",
                    {
                        "attempt": attempt,
                        "channel": rejected,
                        "reason": (
                            "MessageBus rejected delivery "
                            "(disallowed route or no subscriber)"
                        ),
                    },
                )

            # 最終試行でなければ 10 秒待機 (REQ 5.6 の最低間隔)。
            if attempt < _DELIVERY_MAX_ATTEMPTS:
                await asyncio.sleep(_DELIVERY_RETRY_INTERVAL_SECONDS)
