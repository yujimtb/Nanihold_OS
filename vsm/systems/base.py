"""VSM の ``System`` と AgentRuntime 駆動 ``SubAgent`` の基底クラス。

``SubAgent.respond`` はロール別に注入された AgentRuntime を呼び出し、
バックエンド既定またはリクエスト指定のタイムアウトを強制する。成功、
タイムアウト、実行エラーは既存の Event_Log イベント名で記録する。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from vsm.agents.runtime import (
    AgentRequest,
    AgentResult,
    AgentRuntimeError,
    AgentRuntimeProtocol,
)
from vsm.clock import Clock
from vsm.errors import ConfigError, LLMProviderError, LLMTimeoutError
from vsm.eventlog.writer import EventLogWriter
from vsm.ids import generate_uuid
from vsm.messaging.message import Message
from vsm.roles import SystemRole

if TYPE_CHECKING:
    from vsm.memory.builder import ContextViewBuilder
    from vsm.nodes.model import NodeRunState

__all__ = ["SubAgent", "System"]


# REQ 1.4: each System hosts between 1 and 64 Sub_Agent instances. The lower
# bound is verified at Run start by the lifecycle layer (Task 11.1); the
# upper bound is enforced here on every ``register_sub_agent`` call so that
# a misconfigured Run cannot allocate a 65th Sub_Agent at runtime.
_SUB_AGENT_MAX: int = 64


class AgentRuntimeControl(Protocol):
    async def before_agent_invoke(self, node_id: str) -> None: ...

    async def after_agent_invoke(
        self, node_id: str, result: AgentResult, pending_message: Any | None
    ) -> None: ...


@dataclass
class SubAgent:
    """A single LLM-backed agent within a :class:`System`.

    Sub_Agent は 1 つの ``label`` (例: ``"営業"``, ``"リサーチ"``,
    ``"default"``) と 1 つの親 System に紐付き、``respond`` を通じて
    LLM_Provider_Abstraction に prompt を送る。プロバイダー / Event_Log /
    Clock は **コンストラクタ注入** され、テスト時には :class:`FakeClock`
    と :class:`FakeLLMProvider` で完全に決定論化できる。

    Attributes
    ----------
    sub_agent_id : str
        UUIDv4 (32 文字 hex)。``System.register_sub_agent`` が
        :func:`vsm.ids.generate_uuid` で発行する。
    label : str
        人間可読なラベル。S4_Scanner では ``"営業"`` / ``"リサーチ"``
        など (REQ 5.1)、その他 System では ``"default"`` を想定。
    system_id : str
        親 System の ``system_id``。``llm_invocation`` 等の payload に
        ``system_id`` として転写される (REQ 3.3)。
    _runtime : AgentRuntimeProtocol | None
        ロール別 AgentRuntime。決定論ロールでは ``None`` を許容する。
    _eventlog : EventLogWriter
        Event_Log writer。``llm_invocation`` / ``llm_timeout`` /
        ``llm_error`` の 3 経路すべてで本 writer 経由で append する。
    _clock : Clock
        SLA 計測用クロック。``monotonic()`` で経過時間を取り、
        ``elapsed_ms`` を 0 以上の int として保証する。

    Validates Requirements: 1.4, 3.2, 3.3, 3.4, 3.5, 3.6.
    """

    sub_agent_id: str
    label: str
    system_id: str
    _runtime: AgentRuntimeProtocol | None
    _eventlog: EventLogWriter
    _clock: Clock
    _runtime_control: AgentRuntimeControl | None = None
    _run_id: str | None = None
    _run_state: "NodeRunState | None" = None
    _context_builder: "ContextViewBuilder | None" = None
    _resume_within_run: bool = False
    _workdir: Path | None = None

    async def respond(
        self,
        prompt: str,
        context: dict | None = None,
    ) -> AgentResult:
        """LLM_Provider_Abstraction を介して 1 回の応答を返す。

        本メソッドは 3 つの経路を漏れなく Event_Log に記録する:

        1. **正常応答 (REQ 3.3)** — ``asyncio.wait_for`` 内で
           ``self._runtime.invoke(...)`` が成功した場合、``llm_invocation``
           イベントを append し :class:`AgentResult` を呼び出し元に返す。
        2. **タイムアウト (REQ 3.4 / 3.5)** — バックエンドの期限内に応答が返らない
           場合、``asyncio.wait_for`` が ``asyncio.TimeoutError`` を上げる。
           本メソッドはそれを捕捉して ``llm_timeout`` イベントを append
           した後、:class:`LLMTimeoutError` を raise する。Event_Log
           append とエラー伝達は同一イベントループ内で連続的に行われる
           ため、構造的に「キャンセル後 1 秒以内」(REQ 3.5) を満たす。
        3. **実行エラー (REQ 3.6)** — ``self._runtime.invoke`` が
           AgentRuntime の正規化例外を raise した場合、``llm_error``
           イベントを append し、同じ例外を再 raise する。

        Parameters
        ----------
        prompt : str
            ユーザープロンプト本文。
        context : dict | None
            Sub_Agent 呼び出しに付随するメタ情報。``"model"`` キーが
            指定されていればそのモデル名でプロバイダーを呼び出し、
            未指定 (または ``None``) なら Provider の既定モデルを採用
            する (REQ 3.7)。

        Returns
        -------
        AgentResult
            ``text`` / ``model`` / ``latency_ms`` / ``tokens_in`` /
            ``tokens_out`` を含む値オブジェクト。

        Raises
        ------
        LLMTimeoutError
            バックエンド別の期限内に応答が返らなかった (REQ 3.5)。
        LLMProviderError
            プロバイダー側エラーが返った (REQ 3.6)。再 raise であり、
            ``llm_error`` イベントの append 自体は本メソッド内で完了
            している。
        """
        ctx: dict = {} if context is None else context
        # ``context.get("model")`` が None なら Provider の既定モデルが
        # 採用される (REQ 3.7)。テストでは context に ``"model"`` を
        # 明示することで FakeLLMProvider のラベルを切り替えられる。
        model = ctx.get("model")
        runtime = self._runtime
        if runtime is None:
            raise AgentRuntimeError(
                backend="unassigned",
                code="runtime_not_configured",
                message=f"{self.system_id} のロールには AgentRuntime が割り当てられていません",
            )
        timeout = ctx.get("timeout_seconds", runtime.timeout_seconds)
        full_context_view = ctx.get("context_view")
        if full_context_view is None and self._context_builder is not None:
            if self._run_id is None:
                raise RuntimeError("ContextViewBuilder に対応する run_id がありません")
            full_context_view = self._context_builder.build(self.system_id, self._run_id)
        saved_session_ref: str | None = None
        if self._resume_within_run and self._run_state is not None:
            saved_session_ref = self._run_state.session_refs.get(runtime.backend_name)
        requested_workdir = ctx.get("workdir", self._workdir)
        if requested_workdir is None:
            raise RuntimeError(
                f"SubAgent {self.sub_agent_id} の AgentRequest.workdir が未設定です"
            )
        requested_workdir = Path(requested_workdir).resolve(strict=False)
        if self._workdir is not None and requested_workdir != self._workdir:
            raise RuntimeError(
                "SubAgent の workdir は Run が束縛した作業ディレクトリから変更できません: "
                f"expected={self._workdir}, received={requested_workdir}"
            )
        request = AgentRequest(
            prompt=prompt,
            context_view=None if saved_session_ref is not None else full_context_view,
            session_ref=saved_session_ref,
            workdir=requested_workdir,
            model=model,
            timeout_seconds=timeout,
        )

        if self._runtime_control is not None:
            await self._runtime_control.before_agent_invoke(self.system_id)

        # REQ 3.5 の elapsed_ms 計測は壁時計ではなく monotonic clock を
        # 使う。``FakeClock.monotonic`` は ``advance()`` でのみ進むため、
        # PBT 等で「ちょうど 60.1 秒経過」のような決定論的シナリオを
        # 構成できる。
        started_monotonic = self._clock.monotonic()
        tool_invocation_id = generate_uuid()
        await self._eventlog.append(
            "tool_invoked",
            {
                "tool_invocation_id": tool_invocation_id,
                "tool_name": "llm_call",
                "effect": "EXTERNAL_READ",
                "requested_by_node_id": self.system_id,
                "model": model,
            },
            node_id=self.system_id,
            actor_type="agent",
            actor_id=self.sub_agent_id,
        )

        try:
            try:
                response = await asyncio.wait_for(
                    runtime.invoke(request),
                    timeout=timeout,
                )
            except AgentRuntimeError:
                if request.session_ref is None:
                    raise
                # Run 内キャッシュであるセッションが消滅した場合は、同じ
                # 指示を新規セッション + 完全 context view で一度だけ再実行する。
                if self._run_state is not None:
                    self._run_state.session_refs.pop(runtime.backend_name, None)
                request = AgentRequest(
                    prompt=prompt,
                    context_view=full_context_view,
                    session_ref=None,
                    workdir=request.workdir,
                    model=request.model,
                    timeout_seconds=request.timeout_seconds,
                )
                response = await asyncio.wait_for(
                    runtime.invoke(request),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            # REQ 3.4 / 3.5: バックエンド別タイムアウト。``asyncio.wait_for`` が
            # 既に裏側のタスクを cancel しているので、追加の cancel 操作
            # は不要。``elapsed_ms`` は schema の ge=0 制約を満たすため
            # 念のため負値を 0 にクランプする。
            elapsed = self._clock.monotonic() - started_monotonic
            elapsed_ms = max(0, int(elapsed * 1000))
            await self._eventlog.append(
                "llm_timeout",
                {
                    "system_id": self.system_id,
                    "sub_agent_id": self.sub_agent_id,
                    "elapsed_ms": elapsed_ms,
                    "backend": runtime.backend_name,
                    "session_ref": request.session_ref,
                    "tokens_cache_read": 0,
                },
                node_id=self.system_id,
                actor_type="agent",
                actor_id=self.sub_agent_id,
                schema_version=2,
            )
            await self._eventlog.append(
                "tool_failed",
                {
                    "tool_invocation_id": tool_invocation_id,
                    "tool_name": "llm_call",
                    "reason": "timeout",
                    "elapsed_ms": elapsed_ms,
                },
                node_id=self.system_id,
                actor_type="agent",
                actor_id=self.sub_agent_id,
            )
            # ``from None`` で TimeoutError チェーンを切り、呼び出し元には
            # VSM の typed error だけが見えるようにする (design.md
            # §エラー伝播の原則 §典型化)。
            raise LLMTimeoutError(self.sub_agent_id, elapsed) from None
        except (AgentRuntimeError, LLMProviderError) as exc:
            # REQ 3.6: プロバイダーエラー。``provider_code`` schema は
            # min_length=1 の文字列を要求するため、``int | str`` の
            # ``exc.code`` を ``str()`` で正規化する。``provider_message``
            # は schema 上 min_length 制約が無いため空文字も通る。
            code = exc.code
            message = exc.message
            backend = exc.backend if isinstance(exc, AgentRuntimeError) else runtime.backend_name
            await self._eventlog.append(
                "llm_error",
                {
                    "system_id": self.system_id,
                    "sub_agent_id": self.sub_agent_id,
                    "provider_code": str(code),
                    "provider_message": message,
                    "backend": backend,
                    "session_ref": request.session_ref,
                    "tokens_cache_read": 0,
                },
                node_id=self.system_id,
                actor_type="agent",
                actor_id=self.sub_agent_id,
                schema_version=2,
            )
            await self._eventlog.append(
                "tool_failed",
                {
                    "tool_invocation_id": tool_invocation_id,
                    "tool_name": "llm_call",
                    "reason": str(code),
                    "message": message,
                },
                node_id=self.system_id,
                actor_type="agent",
                actor_id=self.sub_agent_id,
            )
            raise

        if self._runtime_control is not None:
            await self._runtime_control.after_agent_invoke(
                self.system_id, response, ctx.get("pending_message")
            )
        if (
            self._resume_within_run
            and self._run_state is not None
            and response.session_ref is not None
        ):
            self._run_state.session_refs[response.backend] = response.session_ref

        # REQ 3.3: 成功経路。``llm_invocation`` payload は AgentResult の
        # フィールドを忠実に転写する (design.md §Event_Log §payload 表)。
        # caller が SLA を観測しやすいよう、append は ``await`` で同期
        # 完了を待ち、戻り値返却前に Event_Log に確実に記録する。
        await self._eventlog.append(
            "llm_invocation",
            {
                "system_id": self.system_id,
                "sub_agent_id": self.sub_agent_id,
                "model": response.model,
                "prompt": prompt,
                "response": response.text,
                "latency_ms": response.latency_ms,
                "tokens_in": response.tokens_in,
                "tokens_out": response.tokens_out,
                "tokens_cache_read": response.tokens_cache_read,
                "backend": response.backend,
                "session_ref": response.session_ref,
            },
            node_id=self.system_id,
            actor_type="agent",
            actor_id=self.sub_agent_id,
            schema_version=2,
        )
        await self._eventlog.append(
            "tool_completed",
            {
                "tool_invocation_id": tool_invocation_id,
                "tool_name": "llm_call",
                "result": {
                    "model": response.model,
                    "latency_ms": response.latency_ms,
                    "tokens_in": response.tokens_in,
                    "tokens_out": response.tokens_out,
                    "tokens_cache_read": response.tokens_cache_read,
                    "backend": response.backend,
                    "session_ref": response.session_ref,
                },
            },
            node_id=self.system_id,
            actor_type="agent",
            actor_id=self.sub_agent_id,
        )
        return response


class System(ABC):
    """Abstract base class for every VSM System (S1〜S5, S3*).

    具体クラス (``S1Worker``, ``S2Coordinator``, ``S3Allocator``,
    ``S3StarAuditor``, ``S4Scanner``, ``S5Policy``; Tasks 12〜17) は
    本クラスを継承し ``run()`` を override する。``run()`` は通常
    着信メッセージのループ + Sub_Agent ディスパッチで構成される。

    Lifecycle
    ---------
    1. ``register_sub_agent(label)`` を必要数呼ぶ (Run 開始前 / 動的生成
       時。1〜64 個 / System; REQ 1.4)。
    2. ``await system.start()`` で ``run()`` を asyncio Task として起動。
    3. ``await system.shutdown()`` で Task を cancel し、終了を待つ。

    Validates Requirements: 1.1, 1.4.

    Parameters
    ----------
    system_id : str
        UUIDv4。Run 内で各 System を識別する一意 ID (REQ 1.5 / 1.6)。
    role : SystemRole
        VSM 上の役割 (S1_WORKER / S2_COORDINATOR / ... ; REQ 1.1)。
    eventlog : EventLogWriter
        Run 単位で共有される Event_Log writer。
    runtime : AgentRuntimeProtocol | None
        Sub_Agent が利用するロール別 AgentRuntime。
    clock : Clock
        Sub_Agent / 内部 SLA 計測で利用するクロック。
    """

    def __init__(
        self,
        *,
        system_id: str,
        role: SystemRole,
        eventlog: EventLogWriter,
        runtime: AgentRuntimeProtocol | None,
        clock: Clock,
    ) -> None:
        self.system_id: str = system_id
        self.role: SystemRole = role
        self._eventlog: EventLogWriter = eventlog
        self._runtime = runtime
        self._clock: Clock = clock
        # ``list`` を内部状態として保持し、``sub_agents`` プロパティ経由で
        # コピーを返すことで外部からの破壊的変更を防ぐ。
        self._sub_agents: list[SubAgent] = []
        # ``run()`` を保持する asyncio Task。``start()`` で生成され、
        # ``shutdown()`` で cancel + await される。
        self._task: asyncio.Task[None] | None = None
        self._instruction_task: asyncio.Task[None] | None = None
        self._instruction_queue: asyncio.Queue[Message] | None = None
        self._runtime_control: AgentRuntimeControl | None = None
        eventlog_path = getattr(eventlog, "_path", None)
        self._workdir: Path | None = (
            Path(eventlog_path).parent.resolve(strict=False)
            if isinstance(eventlog_path, Path)
            else None
        )

    def bind_workdir(self, workdir: Path) -> None:
        """Run が所有する作業ディレクトリを全 SubAgent に束縛する。"""

        resolved = workdir.resolve(strict=False)
        self._workdir = resolved
        for sub_agent in self._sub_agents:
            sub_agent._workdir = resolved

    def bind_runtime_control(self, control: AgentRuntimeControl) -> None:
        """既存および今後登録する SubAgent を Run 制御へ接続する。"""

        self._runtime_control = control
        for sub_agent in self._sub_agents:
            sub_agent._runtime_control = control

    def bind_instruction_queue(self, queue: asyncio.Queue[Message]) -> None:
        """Human からの追加指示を受ける Run 内専用キューを接続する。"""

        if self._instruction_queue is not None and self._instruction_queue is not queue:
            raise RuntimeError(f"instruction queue already bound: {self.system_id}")
        self._instruction_queue = queue

    async def _consume_instructions(self) -> None:
        if self._instruction_queue is None:
            raise RuntimeError(f"instruction queue is not bound: {self.system_id}")
        while True:
            message = await self._instruction_queue.get()
            instruction = message.payload.get("instruction")
            instruction_id = message.payload.get("instruction_id")
            if not isinstance(instruction, str) or not instruction.strip():
                await self._eventlog.append(
                    "instruction_failed",
                    {
                        "instruction_id": instruction_id,
                        "target_node": self.system_id,
                        "reason": "instruction must be a non-empty string",
                    },
                    node_id=self.system_id,
                    actor_id=self.system_id,
                )
                continue
            if not self._sub_agents:
                await self._eventlog.append(
                    "instruction_failed",
                    {
                        "instruction_id": instruction_id,
                        "target_node": self.system_id,
                        "reason": "target Node has no SubAgent",
                    },
                    node_id=self.system_id,
                    actor_id=self.system_id,
                )
                continue
            try:
                response = await self._sub_agents[0].respond(
                    f"人間からの追加指示:\n{instruction.strip()}",
                    context={"pending_message": message},
                )
                await self._eventlog.append(
                    "instruction_completed",
                    {
                        "instruction_id": instruction_id,
                        "target_node": self.system_id,
                        "response": response.text,
                    },
                    node_id=self.system_id,
                    actor_id=self.system_id,
                )
            except Exception as exc:
                await self._eventlog.append(
                    "instruction_failed",
                    {
                        "instruction_id": instruction_id,
                        "target_node": self.system_id,
                        "reason": str(exc),
                    },
                    node_id=self.system_id,
                    actor_id=self.system_id,
                )

    @property
    def sub_agents(self) -> list[SubAgent]:
        """登録済み Sub_Agent のスナップショットコピーを返す。

        外部 (テスト / S3 の specialization 集計など) は本プロパティ経由で
        参照する。返値は ``list`` のコピーなので、呼び出し側の追加 / 削除
        は内部状態に反映されない。
        """
        return list(self._sub_agents)

    @property
    def sub_agent_count(self) -> int:
        """登録済み Sub_Agent 数を返す (``system_instantiated`` payload 用)。"""
        return len(self._sub_agents)

    def register_sub_agent(self, label: str) -> SubAgent:
        """新しい :class:`SubAgent` を本 System に登録して返す。

        REQ 1.4 の上限 (64) を強制する。下限 (1) は Run 開始時の構造検証
        (Task 11.1) で各 System に対し 1 個以上が登録されているかを別途
        確認する設計のため、本メソッドでは 1 個未満の状態で raise する
        ことはしない (空の System を一時的に保持できる)。

        Parameters
        ----------
        label : str
            Sub_Agent ラベル。Event_Log の ``llm_invocation`` payload では
            ``sub_agent_id`` を主キーとしてトレースするため、本ラベルは
            主に CLI / 診断ログでの可読性向上のために使う。

        Returns
        -------
        SubAgent
            新規生成された Sub_Agent。``sub_agent_id`` は UUIDv4 の hex
            (32 文字)、``system_id`` は本 System の ``system_id``、
            プロバイダー / Event_Log / Clock は本 System の依存を共有する。

        Raises
        ------
        ConfigError
            既に 64 個の Sub_Agent が登録されているのに追加が要求された
            場合 (REQ 1.4 上限超過)。``missing_roles`` は空リスト、
            ``detail`` には System 役割 / system_id / 上限値を含めて
            原因を一意に特定できるメッセージを格納する。
        """
        if len(self._sub_agents) >= _SUB_AGENT_MAX:
            # REQ 1.4: 上限 64 を超える Sub_Agent 登録は構造制約違反。
            # ``ConfigError`` は ``missing_roles`` フィールドを必須とする
            # ため、上限超過ケースでは空リストを渡し ``detail`` に詳細を
            # 集約する (design.md §Error Handling §例外階層)。
            raise ConfigError(
                missing_roles=[],
                detail=(
                    f"System {self.role.value}/{self.system_id} already "
                    f"hosts {_SUB_AGENT_MAX} Sub_Agents (REQ 1.4 upper "
                    f"bound exceeded)"
                ),
            )

        sub_agent = SubAgent(
            sub_agent_id=generate_uuid(),
            label=label,
            system_id=self.system_id,
            _runtime=self._runtime,
            _eventlog=self._eventlog,
            _clock=self._clock,
            _runtime_control=self._runtime_control,
            _workdir=self._workdir,
        )
        self._sub_agents.append(sub_agent)
        return sub_agent

    def bind_node_context(
        self,
        *,
        run_id: str,
        run_state: "NodeRunState",
        context_builder: "ContextViewBuilder",
        resume_within_run: bool,
    ) -> None:
        """全 SubAgent を、この System に対応する Run/Node 状態へ結び付ける。"""
        for sub_agent in self._sub_agents:
            sub_agent._run_id = run_id
            sub_agent._run_state = run_state
            sub_agent._context_builder = context_builder
            sub_agent._resume_within_run = resume_within_run

    @abstractmethod
    async def run(self) -> None:
        """System のメインループ。サブクラスが override する。

        実装は通常、自分宛のチャネルを subscribe し、受信メッセージを
        Sub_Agent に dispatch するループとなる。``shutdown()`` 経由で
        cancel されることを前提に、``asyncio.CancelledError`` を捕捉
        せず再 raise / 伝搬させること。
        """
        ...

    async def start(self) -> None:
        """``run()`` を asyncio Task として起動する。

        本メソッドはアイドル状態 (``_task is None`` または ``done()``)
        のときのみ Task を生成する。二重起動は no-op であり、シャットダウン
        途中で再 start を呼ぶようなパスでも安全に動作する。Task 名は
        ``"<role>[<system_id>]"`` 形式で、``asyncio.all_tasks()`` 経由の
        診断やデバッグ時のトレーサビリティを確保する。
        """
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self.run(),
                name=f"{self.role.value}[{self.system_id}]",
            )
        if self._instruction_queue is not None and (
            self._instruction_task is None or self._instruction_task.done()
        ):
            self._instruction_task = asyncio.create_task(
                self._consume_instructions(),
                name=f"instruction[{self.system_id}]",
            )

    async def shutdown(self) -> None:
        """``run()`` Task を cancel し、終了を待つ。

        ``run()`` は通常 ``while True`` ループのため、``cancel()`` 後に
        ``await`` すると :class:`asyncio.CancelledError` が伝播する。本
        メソッドは想定された経路としてこれを握り潰し、呼び出し側を
        例外なくシャットダウンさせる。Task が無い / 既に done の場合は
        no-op として安全に呼べる (lifecycle 層からの冗長呼び出しに耐える)。
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # ``run()`` のキャンセルは正常終了として扱う。
                pass
        # ``done()`` 判定後も次回 ``start()`` で再生成できるようリセット。
        self._task = None
        if self._instruction_task is not None and not self._instruction_task.done():
            self._instruction_task.cancel()
            try:
                await self._instruction_task
            except asyncio.CancelledError:
                pass
        self._instruction_task = None
