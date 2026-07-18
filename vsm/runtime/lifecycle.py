"""Run Lifecycle: structural verification, Run dir creation, Platform orchestration.

design.md `## Architecture` の :class:`Platform` (single asyncio event loop の
オーケストレータ) と `## Components and Interfaces` §7 (Run Lifecycle /
構造制約検証) に対応する実装。本モジュールは Run の **起動 / 終了 / 動的
S1_Worker 生成** を担い、CLI からは :func:`start_run` を通じて呼び出される。

Run start sequence
------------------
1. **構造検証 (REQ 13.1)** — :data:`MANDATORY_ROLES` の各役割について
   ``RunConfig.count(role) >= 1`` を確認する。違反があれば *Run dir を
   作る前に* :class:`ConfigError` を raise し、stderr に
   ``missing required systems: <names>`` を書く (REQ 13.2 / 13.3)。CLI 層
   が exit code 3 に翻訳する。
2. **Run dir / events.jsonl / RUNNING lockfile 作成 (REQ 10.3, 11.6)** —
   ``runs/{run_id}/`` を ``mkdir(exist_ok=False)`` で作成し、
   ``events.jsonl`` と ``RUNNING`` を ``touch`` する。失敗時は
   :class:`RunDirectoryError` を raise (REQ 10.4)。CLI 層が exit code 4
   に翻訳する。``RUNNING`` lockfile は ``vsm replay`` が active Run を
   検出するために使う (REQ 11.6)。
3. **EventLogWriter / AgentRuntime / MessageBus 構築** — Run スコープの
   依存をすべて 1 か所で組み立て、ロール別 runtime を解決する。
4. **必須 System の生成 + ``system_instantiated`` event 発行 (REQ 1.5)**
   — 各役割について **1 つの System インスタンス** を作り、Run 開始
   から 5 秒以内に ``system_instantiated`` event を append する。
   Sub_Agent の登録は **概念的に 2 段階**:

   a. **概念上の責務**: 概念上、各 System は ``RunConfig.count(role)``
      個の Sub_Agent をホストする。本数値は ``system_instantiated``
      payload の ``sub_agent_count`` に転写される (REQ 1.5)。
   b. **実体上の責務**: 実際の Sub_Agent 登録は **概念上、各概念
      System の ``__init__``** が担う (REQ 5.1 の S4 における 営業 /
      リサーチ Sub_Agent のような役割固有の Sub_Agent ラベル割当が
      必要なため)。Lifecycle 層は一切の役割固有知識を持たず、
      defensively, 概念 System が 1 つも Sub_Agent を登録しなかった
      場合のみ 1 つだけ ``"default"`` ラベルの Sub_Agent を登録する。
      Tasks 12〜17 の各 System 実装は ``RunConfig.count(role)`` 個の
      Sub_Agent を自身の ``__init__`` で登録する責務を負う。

5. **Run の active 化** — :meth:`Platform.start` で各 System の ``run()``
   coroutine を asyncio Task として起動する。

Concrete System constructor contract (Tasks 12〜17)
---------------------------------------------------
Tasks 12〜17 で実装される ``S1Worker``, ``S2Coordinator``, ``S3Allocator``,
``S3StarAuditor``, ``S4Scanner``, ``S5Policy`` の各クラスは以下の
キーワード専用シグネチャに従わなければならない (lifecycle はこの順で
インスタンス化する)::

    class S5Policy(System):
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
            # 必要に応じて role 固有の追加 kwarg
        ) -> None: ...

S1Worker / S3Allocator / S3StarAuditor は加えて ``specialization: str | None``
の kwarg を受け取って良い (動的生成時に :meth:`Platform.spawn_s1` が渡す)。
``platform`` 参照は S3_Allocator が ``platform.spawn_s1(...)`` を呼んで
S1 を動的生成するための入口で、循環参照を避けるため lazy import で配線する。

Validates Requirements
----------------------
REQ 1.1, 1.2, 1.3, 1.5, 1.6, 1.7, 10.3, 10.4, 11.6, 13.1, 13.2, 13.3,
13.4, 13.5, 13.6.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from vsm.agents.backends import (
    ClaudeCodeRuntime,
    CodexRuntime,
    FakeAgentRuntime,
    LiteLLMRuntimeAdapter,
)
from vsm.agents.runtime import AgentRequest, AgentResult, AgentRuntimeProtocol
from vsm.authority import ParentAuthority
from vsm.budget import InvocationBudgetGuard
from vsm.clock import Clock, SystemClock
from vsm.config import LLMConfig, RunConfig
from vsm.errors import (
    BudgetExceededError,
    ConfigError,
    QuotaExhaustedError,
    QuotaResolutionRequiredError,
    RunDirectoryError,
    SystemInstantiationError,
    WorkspaceError,
)
from vsm.eventlog.writer import EventLogWriter
from vsm.eventlog.reader import read_all
from vsm.ids import generate_run_id, generate_uuid
from vsm.llm.types import LLMProviderProtocol
from vsm.lethe_bridge import (
    LetheBridge,
    LetheContextInjector,
    LetheTransport,
    SupplementalRecord,
)
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId, ExternalRole
from vsm.messaging.message import Message, SendResult
from vsm.nodes import (
    DifferentiationLevel,
    Node,
    NodeRunState,
    NodeSource,
    NodeStatus,
    transition_node_status,
)
from vsm.roles import MANDATORY_ROLES, RoleSpec, SystemRole
from vsm.runtime.consortium import (
    Consortium,
    ConsortiumDecision,
    ContextViewHook,
    HumanStatementWaiter,
    NodeParticipant,
)
from vsm.runtime.quota import QuotaMonitor
from vsm.runtime.manifest import RunManifest, WorkspaceController
from vsm.tools import (
    AlgedonicFacade,
    AlgedonicRequest,
    EscalationFacade,
    EscalationRequest,
    SpawnChildFacade,
    SpawnChildRequest,
    SpawnChildResult,
    ToolEffect,
    ToolInvocation,
)

if TYPE_CHECKING:
    # 型注釈用のみ。実体クラスは Tasks 12〜17 で実装されるため、
    # 実行時 import は :meth:`Platform.create` / :meth:`Platform.spawn_s1`
    # 内で遅延させて循環参照と未実装エラーを回避する。
    from vsm.systems.base import System


__all__ = ["Platform", "start_run"]


# REQ 10.3: Run ディレクトリの既定親パス。CLI からは絶対パスを渡しうる
# が、PoC 既定では CWD 相対の ``runs/`` を使う。
_DEFAULT_RUNS_DIR = Path("runs")

# REQ 11.6: ``vsm replay`` が active Run を検出するための lockfile 名。
# Run 中は存在し、:meth:`Platform.shutdown` で削除される。
_RUNNING_LOCKFILE_NAME = "RUNNING"

# Run 内のイベントログファイル名 (REQ 10.3)。
_EVENTS_FILENAME = "events.jsonl"


# Run start 時に各 System が購読するインバウンドチャネル一覧。各 System の
# ``run()`` ループ冒頭で行われる ``bus.subscribe`` と一致させる必要があり、
# 任意の System の ``run()`` が起動するより *前に* :class:`Platform.create`
# が事前 subscribe するために使う。これがないと、すぐに別 System が
# ``send`` した場合に subscribe 前で ``channel_rejected`` になるレースが
# 発生する (S1_S3 / S4_S5 / S3_S5 すべての channel で再現する)。
# ``S1_WORKER`` は :meth:`spawn_s1` 内で同等の事前 subscribe を行う。
_ROLE_INBOUND_CHANNELS: dict[SystemRole, tuple[ChannelId, ...]] = {
    SystemRole.S2_COORDINATOR: (ChannelId.S1_S2,),
    SystemRole.S3_ALLOCATOR: (ChannelId.S3_S5, ChannelId.S1_S3),
    SystemRole.S3STAR_AUDITOR: (),  # S3* は受信専用チャネルを持たない (送信のみ)
    SystemRole.S4_SCANNER: (ChannelId.S4_S5,),
    SystemRole.S5_POLICY: (
        ChannelId.S4_S5,
        ChannelId.S3_S5,
        ChannelId.S3STAR_S5_AUDIT,
    ),
}


def _role_spec_for_system_role(role: SystemRole) -> RoleSpec:
    return RoleSpec(
        id=role.value,
        vsm_position=role,
        responsibility=f"{role.value} responsibility",
        allowed_tools=(
            "llm_call",
            "codex_run",
            "spawn_child",
            "differentiate",
            "search_past_subtasks",
            "request_coordination",
            "request_escalation",
            "request_human_review",
            "terminate_node",
            "suspend_node",
            "resume_node",
        ),
    )


class Platform:
    """Single-Run orchestrator (design.md §Architecture).

    1 つの Run を所有し、その間に存在する :class:`EventLogWriter`、
    :class:`MessageBus`、ロール別 AgentRuntime、および全 System インスタンスの
    ライフサイクルを管理する。``asyncio.run`` で起動した呼び出し側が
    所有する asyncio event loop の上で動作する (Platform 自身は loop を
    起動しない)。

    Attributes
    ----------
    run_id : str
        この Run を一意に識別する文字列。:func:`vsm.ids.generate_run_id`
        が ``run-<32 hex>`` 形式で生成する (REQ 4.6, 10.2)。
    run_dir : Path
        ``runs/{run_id}/`` の絶対 / 相対パス。``events.jsonl`` と
        ``RUNNING`` lockfile がここに作られる (REQ 10.3, 11.6)。
    eventlog : EventLogWriter
        Run 単位の単一 writer。:meth:`shutdown` で停止する。
    bus : MessageBus
        Run 内の全 System をつなぐメッセージバス。
    runtimes : Mapping[SystemRole, AgentRuntimeProtocol | None]
        Sub_Agent が呼び出すロール別 AgentRuntime。
    clock : Clock
        SLA 計測用クロック。本番は :class:`SystemClock`、テストでは
        :class:`FakeClock` を差し込める。
    run_config : RunConfig
        Run 開始時の構造設定 (Sub_Agent 数 / S1 上限など, REQ 13)。
    systems : dict[SystemRole, list[System]]
        役割ごとの System インスタンス一覧。S1_WORKER のリストは
        :meth:`spawn_s1` が動的に増やしていく (REQ 1.6, 13.6)。

    Validates Requirements: 1.1, 1.5, 13.1, 13.6.
    """

    def __init__(
        self,
        *,
        run_id: str,
        run_dir: Path,
        eventlog: EventLogWriter,
        bus: MessageBus,
        runtimes: Mapping[SystemRole, AgentRuntimeProtocol | None],
        clock: Clock,
        run_config: RunConfig,
        manifest: RunManifest | None = None,
        workspace_controller: WorkspaceController | None = None,
        workdir: Path | None = None,
        context_view_hook: ContextViewHook | None = None,
        human_statement_waiter: HumanStatementWaiter | None = None,
        lethe_bridge: LetheBridge,
        metering_hook: Callable[[str, str, AgentResult], Any] | None = None,
    ) -> None:
        self.run_id: str = run_id
        self.run_dir: Path = run_dir
        self.eventlog: EventLogWriter = eventlog
        self.bus: MessageBus = bus
        self.runtimes = dict(runtimes)
        self.clock: Clock = clock
        self.run_config: RunConfig = run_config
        self.lethe_bridge = lethe_bridge
        self.lethe_context_records: tuple[SupplementalRecord, ...] = ()
        self.manifest = manifest
        self.workspace_controller = workspace_controller
        self.metering_hook = metering_hook
        if workdir is None:
            raise ValueError("Platform の workdir は明示的に指定しなければなりません")
        self.workdir: Path = workdir.resolve(strict=False)
        self._active_s1_writer: str | None = None

        # 役割別 System インスタンス。``MANDATORY_ROLES`` 各役割に対して
        # 1 つずつ、S1_WORKER については :meth:`spawn_s1` が動的に追加。
        self.systems: dict[SystemRole, list[System]] = {}

        # Refactor 20260608: Systems remain the execution adapter, while Node
        # owns persistent responsibility/history/authority.
        self.nodes: dict[str, Node] = {}
        self.node_run_states: dict[tuple[str, str], NodeRunState] = {}
        self._node_pools: dict[str, str] = {}
        self.authorities: dict[str, ParentAuthority] = {}
        self.tool_invocations: dict[str, ToolInvocation] = {}
        self.run_cost_consumed: dict[str, float] = {
            "tokens_in": 0.0,
            "tokens_out": 0.0,
            "tokens_cache_read": 0.0,
            "tokens_total": 0.0,
            "wall_clock_ms": 0.0,
            "node_running_ms": 0.0,
        }
        self._node_started_at: dict[str, float] = {}
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_task: asyncio.Task[None] | None = None
        self._escalations = EscalationFacade()
        self._budget_guard = InvocationBudgetGuard(
            initial_tokens=run_config.budget.invocation_initial_tokens,
            initial_wall_clock_seconds=(
                run_config.budget.invocation_initial_wall_clock_seconds
            ),
            safety_multiplier=run_config.budget.invocation_safety_multiplier,
        )
        self.quota_monitor = QuotaMonitor(
            eventlog=eventlog,
            bus=bus,
            clock=clock,
            nodes=self.nodes,
            node_run_states=self.node_run_states,
            run_id=run_id,
            node_pools=self._node_pools,
            state_path=run_dir / "quota-state.json",
            probe=self._quota_probe,
            on_node_suspended=self._pause_quota_accounting,
            on_node_resumed=self._resume_quota_accounting,
        )

        from vsm.memory.builder import ContextViewBuilder
        from vsm.tools.search import TaskSummaryIndex

        self.task_summary_index = TaskSummaryIndex(run_dir / "memory" / "task-summaries.jsonl")
        self.context_view_builder = ContextViewBuilder(
            nodes=self.nodes,
            events_path=run_dir / _EVENTS_FILENAME,
            summary_index=self.task_summary_index,
            run_dir=run_dir,
        )
        async def resolved_context_view_hook(
            node_id: str,
            hook_run_id: str,
            subject: str,
            recent_event_summary: str,
        ) -> str:
            if context_view_hook is None:
                base = self.context_view_builder.build(node_id, hook_run_id)
            else:
                value = context_view_hook(
                    node_id, hook_run_id, subject, recent_event_summary
                )
                base = await value if inspect.isawaitable(value) else value
            if not self.lethe_context_records:
                return base
            return "\n".join(
                [
                    base,
                    "【LETHE Run間文脈】",
                    *(f"- {record.text}" for record in self.lethe_context_records),
                ]
            )
        self.consortium = Consortium(
            run_id=run_id,
            eventlog=eventlog,
            config=run_config.consortium,
            context_view_hook=resolved_context_view_hook,
            human_statement_waiter=human_statement_waiter,
        )

        # 動的 S1 生成回数のカウンタ。``s1_dynamic_max`` (REQ 13.6) と
        # ``s1_max`` (REQ 1.3) の双方を :meth:`spawn_s1` が確認する。
        # ``current`` ではなく **総生成数** をカウントする (REQ 13.6 が
        # 「concurrent instances」を要求する一方、PoC では terminate 時に
        # decrement する経路がまだないため、現実的に総量で代用する。
        # 後続 task で terminate 経路が追加されたら同期的に decrement
        # する設計にすること)。
        self._s1_count: int = 0

    async def load_lethe_context(
        self,
        query: str | None,
        injector: LetheContextInjector | None = None,
    ) -> tuple[SupplementalRecord, ...]:
        """Run 開始時に関連レコードを検索し、文脈 hook へ注入する。"""

        if not self.run_config.lethe.enabled or query is None:
            return ()

        def store(records: Sequence[SupplementalRecord]) -> None:
            self.lethe_context_records = tuple(records)

        records = await self.lethe_bridge.inject_context(query, store)
        if injector is not None:
            result = injector(records)
            if inspect.isawaitable(result):
                await result
        return tuple(records)

    def reserve_s1_writer(self, system: "System") -> None:
        """同一 self-hosting worktree の S1 writer を 1 つに制限する。"""

        if self.workspace_controller is None:
            return
        runtime = getattr(system, "_runtime", None)
        backend = getattr(runtime, "backend_name", None)
        if not backend or backend == "litellm":
            return
        owner = self._active_s1_writer
        if owner is not None and owner != system.system_id:
            raise WorkspaceError(
                "1 Run 1 writer 違反: "
                f"worktree={self.workdir} は S1 {owner} が使用中で、"
                f"S1 {system.system_id} を同時割り当てできません"
            )
        self._active_s1_writer = system.system_id

    def release_s1_writer(self, system: "System") -> None:
        """S1 の通常完了後に writer 使用権を解放する。"""

        if self._active_s1_writer == system.system_id:
            self._active_s1_writer = None

    def _pause_quota_accounting(self, node_id: str) -> None:
        started = self._node_started_at.pop(node_id, None)
        if started is None:
            return
        elapsed_ms = max(0.0, (self.clock.monotonic() - started) * 1000)
        state = self.node_run_states[(self.run_id, node_id)]
        state.cost_consumed["node_running_ms"] += elapsed_ms
        self.run_cost_consumed["node_running_ms"] += elapsed_ms

    def _resume_quota_accounting(self, node_id: str) -> None:
        self._node_started_at[node_id] = self.clock.monotonic()

    async def _quota_probe(self, pool: str) -> bool:
        """pool ごとに一つだけ軽量 probe を送る。"""

        if pool.startswith("node:"):
            return True
        node_id = next(
            node_id for node_id, node_pool in self._node_pools.items() if node_pool == pool
        )
        node = self.nodes[node_id]
        if not isinstance(node.vsm_position, SystemRole):
            raise RuntimeError(f"quota pool {pool} has no SystemRole node")
        runtime = self.runtimes.get(node.vsm_position)
        if runtime is None:
            raise RuntimeError(f"quota pool {pool} has no AgentRuntime")
        result = await runtime.invoke(
            AgentRequest(
                prompt="Nanihold quota health probe: return a short readiness response.",
                workdir=self.workdir,
                timeout_seconds=min(runtime.timeout_seconds, 30.0),
            )
        )
        return not result.quota_exhausted

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        *,
        run_id: str,
        runs_dir: Path = _DEFAULT_RUNS_DIR,
        run_config: RunConfig | None = None,
        manifest: RunManifest | None = None,
        llm_config: LLMConfig | None = None,
        llm_override: LLMProviderProtocol | None = None,
        runtime_overrides: Mapping[SystemRole, AgentRuntimeProtocol | None] | None = None,
        clock: Clock | None = None,
        context_view_hook: ContextViewHook | None = None,
        human_statement_waiter: HumanStatementWaiter | None = None,
        lethe_transport: LetheTransport | None = None,
        lethe_context_query: str | None = None,
        lethe_context_injector: LetheContextInjector | None = None,
        metering_hook: Callable[[str, str, AgentResult], Any] | None = None,
        resume: bool = False,
    ) -> "Platform":
        """Construct and bootstrap a :class:`Platform` for a new Run.

        実行順序:

        1. **構造検証** (REQ 13.1〜13.3): ``MANDATORY_ROLES`` の各役割に
           ついて ``run_config.count(role) >= 1`` を確認する。違反時は
           ``missing required systems: <comma-sep>`` を stderr に書き、
           :class:`ConfigError` を raise する (Run dir は作らない)。
        2. **Run dir / events.jsonl / RUNNING lockfile 作成** (REQ 10.3,
           10.4, 11.6): ``runs/{run_id}/`` を ``mkdir(exist_ok=False)``
           で作る。同名ディレクトリがすでに存在する / I/O エラー /
           権限不足時は :class:`RunDirectoryError` を raise する。
        3. **EventLogWriter / AgentRuntime / MessageBus の構築と起動**.
        4. **必須 System の生成 + ``system_instantiated`` event 発行**
           (REQ 1.5): 役割ごとに 1 つの System を作り、概念 System の
           ``__init__`` が登録した Sub_Agent 数を ``sub_agent_count``
           payload に転写する。万一 Sub_Agent が 0 個の状態で渡って
           きたら defensively 1 つだけ ``"default"`` ラベルで登録する。
        5. ステップ 4 の途中でエラーが起きた場合は **best-effort cleanup**
           (writer 停止 + lockfile / Run dir 削除) を行ってから
           :class:`SystemInstantiationError` を再 raise する (REQ 1.7)。

        Parameters
        ----------
        run_id : str
            Run 識別子。``runs/{run_id}/`` ディレクトリ名にもなる。
        runs_dir : Path
            Run ディレクトリ群の親。既定 ``Path("runs")`` (CWD 相対)。
        run_config : RunConfig | None
            Sub_Agent 数 / S1 上限などの構造設定。``None`` なら既定値。
        llm_config : LLMConfig | None
            LLM プロバイダー設定 (REQ 3.7)。``llm_override`` が非 ``None``
            なら無視される。
        llm_override : LLMProviderProtocol | None
            テスト用にプロバイダーを直接差し込む経路。本番経路では使わない。
        clock : Clock | None
            ``None`` なら :class:`SystemClock` を採用。テストでは
            :class:`FakeClock` を渡す。

        Returns
        -------
        Platform
            起動準備完了した Platform。:meth:`start` を呼ぶと各 System
            の asyncio Task が走り出す。

        Raises
        ------
        ConfigError
            必須 System が不足している (REQ 13.1〜13.3)。CLI 層が
            exit code 3 に翻訳する。
        RunDirectoryError
            Run ディレクトリ / ``events.jsonl`` の作成に失敗した
            (REQ 10.4)。CLI 層が exit code 4 に翻訳する。
        SystemInstantiationError
            必須 System のインスタンス化に失敗した (REQ 1.7)。
        """
        rc = run_config or RunConfig()
        run_dir = runs_dir / run_id
        if resume and manifest is None and (run_dir / "manifest.json").exists():
            manifest = RunManifest.load(run_dir)

        if rc.selfdev.enabled and manifest is None:
            raise ConfigError(
                missing_roles=[],
                detail="selfdev.enabled の Run には RunManifest が必要です",
            )
        if manifest is not None and manifest.run_id != run_id:
            raise ConfigError(
                missing_roles=[],
                detail=(
                    f"RunManifest.run_id={manifest.run_id!r} は "
                    f"Run の run_id={run_id!r} と一致しません"
                ),
            )
        if (
            rc.selfdev.enabled
            and manifest is not None
            and manifest.repository != rc.selfdev.repository
        ):
            raise ConfigError(
                missing_roles=[],
                detail=(
                    "RunManifest.repository が [selfdev] repository と一致しません: "
                    f"manifest={manifest.repository}, configured={rc.selfdev.repository}"
                ),
            )

        # ------------------------------------------------------------------
        # 1. 構造検証 (REQ 13.1〜13.3)
        # ------------------------------------------------------------------
        # 不足役割は ``SystemRole.value`` で報告する (例: ``"S2_COORDINATOR"``)。
        # ``MANDATORY_ROLES`` の順序は :class:`frozenset` のため定義順に
        # 整列されないので、stderr メッセージの可読性向上のため
        # :class:`SystemRole` の定義順で並び替える。
        ordered_mandatory = [
            role for role in SystemRole if role in MANDATORY_ROLES
        ]
        missing = [role.value for role in ordered_mandatory if rc.count(role) < 1]
        if missing:
            # REQ 13.3: stderr に missing 全件を含むメッセージを書く。
            # 改行付きの単一行フォーマットで、CLI / property test が
            # 機械的に検証できる。
            print(
                f"missing required systems: {', '.join(missing)}",
                file=sys.stderr,
            )
            # REQ 13.2: Run dir を作らずに abort。CLI 層は ``ConfigError``
            # を捕捉して ``sys.exit(3)`` を実行する。
            raise ConfigError(
                missing_roles=missing,
                detail=(
                    "mandatory Systems missing at Run start "
                    f"(REQ 13.1): {', '.join(missing)}"
                ),
            )

        if llm_override is not None and runtime_overrides is not None:
            raise ConfigError(
                missing_roles=[],
                detail="llm_override と runtime_overrides は同時に指定できません",
            )
        runtimes = _resolve_role_runtimes(
            run_config=rc,
            llm_config=llm_config or LLMConfig(),
            llm_override=llm_override,
            runtime_overrides=runtime_overrides,
        )

        # ------------------------------------------------------------------
        # 2. Run dir / events.jsonl / RUNNING lockfile 作成 (REQ 10.3, 11.6)
        # ------------------------------------------------------------------
        events_path = run_dir / _EVENTS_FILENAME
        lockfile_path = run_dir / _RUNNING_LOCKFILE_NAME
        resuming_existing = resume and run_dir.exists()
        try:
            if resuming_existing:
                if not events_path.is_file():
                    raise RunDirectoryError(
                        f"cannot resume run without {events_path}"
                    )
            else:
                # 新規 Run は従来どおり既存ディレクトリを上書きしない。
                run_dir.mkdir(parents=True, exist_ok=False)
                events_path.touch()
            # REQ 11.6: active Run の検出マーカ。``vsm replay`` が
            # 存在を見て stderr に warning を先出しする。
            lockfile_path.touch()
        except OSError as exc:
            # REQ 10.4: stderr にパスと理由を書き、典型例外で raise。
            # CLI 層は exit code 4 に翻訳する。
            print(
                f"failed to create run directory {run_dir}: {exc}",
                file=sys.stderr,
            )
            raise RunDirectoryError(
                f"failed to create run directory {run_dir}: {exc}"
            ) from exc

        # ------------------------------------------------------------------
        # 3. EventLogWriter / AgentRuntime / MessageBus 構築
        # ------------------------------------------------------------------
        # ``clock`` injection でテスト時の決定論を確保する。
        clk: Clock = clock if clock is not None else SystemClock()

        resume_system_ids: dict[SystemRole, list[str]] = {}
        if resuming_existing:
            for event in read_all(events_path):
                if event.get("event_type") != "system_instantiated":
                    continue
                payload = event.get("payload") or {}
                try:
                    role = SystemRole(str(payload["role"]))
                    system_id = str(payload["system_id"])
                except (KeyError, ValueError):
                    continue
                if system_id not in resume_system_ids.setdefault(role, []):
                    resume_system_ids[role].append(system_id)

        # writer は他の依存より先に start し、後段でエラーが起きても
        # ``_cleanup_partial_run`` が確実に stop できるようにする。
        writer = EventLogWriter(run_id=run_id, path=events_path, clock=clk)
        await writer.start()

        # MessageBus は writer に依存する (channel_message / channel_rejected
        # を append するため)。
        bus = MessageBus(eventlog=writer)

        workspace_controller: WorkspaceController | None = None
        if manifest is not None:
            workspace_controller = WorkspaceController(manifest=manifest, run_dir=run_dir)
        try:
            if workspace_controller is not None:
                manifest.persist(run_dir)
                workdir = (
                    run_dir / "workspace"
                    if resuming_existing and (run_dir / "workspace").is_dir()
                    else workspace_controller.start()
                )
            else:
                workdir = run_dir / "workspace"
                workdir.mkdir(parents=True, exist_ok=resuming_existing)
        except Exception:
            await _cleanup_partial_run(
                writer=writer,
                run_dir=run_dir,
                lockfile_path=lockfile_path,
                workspace_controller=workspace_controller,
            )
            raise

        # ------------------------------------------------------------------
        # 4. 必須 System の生成 + ``system_instantiated`` event (REQ 1.5)
        # ------------------------------------------------------------------
        # Concrete System クラスは Tasks 12〜17 で実装される。実装が揃うまで
        # ``import`` 自体が ``ImportError`` になりうるため、import は
        # **メソッド内で遅延** させる (本モジュールの py_compile / 単体
        # import が壊れない)。Tasks 12〜17 のいずれかが未実装の状態で
        # 本メソッドを実行すると、当該 import で ``ImportError`` が
        # raise され、下記 ``except Exception`` で捕捉して clean up する。
        platform = cls(
            run_id=run_id,
            run_dir=run_dir,
            eventlog=writer,
            bus=bus,
            runtimes=runtimes,
            clock=clk,
            run_config=rc,
            manifest=manifest,
            workspace_controller=workspace_controller,
            workdir=workdir,
            context_view_hook=context_view_hook,
            human_statement_waiter=human_statement_waiter,
            lethe_bridge=LetheBridge(config=rc.lethe, transport=lethe_transport),
            metering_hook=metering_hook,
        )

        try:
            await platform.load_lethe_context(
                lethe_context_query, injector=lethe_context_injector
            )
            await writer.append(
                "budget_configured",
                {
                    "run_tokens": rc.budget.run_tokens,
                    "run_wall_clock_seconds": rc.budget.run_wall_clock_seconds,
                    "invocation_initial_tokens": rc.budget.invocation_initial_tokens,
                    "invocation_initial_wall_clock_seconds": (
                        rc.budget.invocation_initial_wall_clock_seconds
                    ),
                    "invocation_safety_multiplier": (
                        rc.budget.invocation_safety_multiplier
                    ),
                },
                actor_id="platform",
            )
            # 遅延 import: Tasks 12〜17 で実装される。
            from vsm.systems.s2_coordinator import S2Coordinator  # noqa: F401
            from vsm.systems.s3_allocator import S3Allocator  # noqa: F401
            from vsm.systems.s3star_auditor import S3StarAuditor  # noqa: F401
            from vsm.systems.s4_scanner import S4Scanner  # noqa: F401
            from vsm.systems.s5_policy import S5Policy  # noqa: F401

            # 役割と具象クラスのマップ。MANDATORY_ROLES と要素が一致する。
            # S1_WORKER はここには含めない (REQ 13.5: Run start 時点では
            # 0 個でよく、:meth:`spawn_s1` で動的に増やす)。
            role_to_class: dict[SystemRole, type[System]] = {
                SystemRole.S2_COORDINATOR: S2Coordinator,
                SystemRole.S3_ALLOCATOR: S3Allocator,
                SystemRole.S3STAR_AUDITOR: S3StarAuditor,
                SystemRole.S4_SCANNER: S4Scanner,
                SystemRole.S5_POLICY: S5Policy,
            }

            # MANDATORY_ROLES が role_to_class とずれていないか早期検証。
            # 設計上同一であるはずだが、後続 task の追加削除で齟齬が出る
            # ことを防ぐためのプログラマティックなアサーション。
            assert set(role_to_class.keys()) == MANDATORY_ROLES, (
                "role_to_class must enumerate exactly MANDATORY_ROLES; "
                f"got {set(role_to_class.keys())} vs {MANDATORY_ROLES}"
            )

            # 各役割について 1 つの System を生成する (1 System per role
            # モデル, REQ 1.2)。Sub_Agent 数は概念 System の ``__init__``
            # が ``run_config.count(role)`` 個を登録する責務を負う。
            for role, system_cls in role_to_class.items():
                # 具象 System コンストラクタは本モジュール先頭の docstring
                # に示した kwarg-only シグネチャに従う (Tasks 12〜17 の契約)。
                existing_ids = resume_system_ids.get(role, [])
                instance: System = system_cls(  # type: ignore[call-arg]
                    system_id=(existing_ids[0] if existing_ids else generate_uuid()),
                    eventlog=writer,
                    bus=bus,
                    runtime=runtimes[role],
                    clock=clk,
                    platform=platform,
                    run_config=rc,
                )

                # Defensive: 概念 System が 1 つも Sub_Agent を登録しなかった
                # 場合は最小 1 個 (REQ 1.4 下限) を保証する。Tasks 12〜17 は
                # 自身の ``__init__`` で ``run_config.count(role)`` 個を
                # 登録すべきだが、未実装段階の暫定値として "default" を
                # 用意する。
                if instance.sub_agent_count == 0:
                    instance.register_sub_agent(label="default")

                instance.bind_workdir(workdir)

                platform.systems.setdefault(role, []).append(instance)
                await platform._attach_system_node(instance, role)

                # **REQ 2.x の隠れた前提**: System の ``run()`` ループは
                # 起動後の最初の文で ``bus.subscribe`` を呼ぶ。だが
                # ``Platform.start()`` が ``asyncio.create_task`` を経由する
                # ため、別の System が ``send`` する瞬間に subscribe が
                # まだ走っていないと :class:`MessageBus.send` は受信者
                # キュー未登録として ``channel_rejected`` を返す。これを
                # 避けるため、``Platform.start()`` で run task を起こす
                # *前* に、各 System が購読すべきチャネルを事前
                # ``subscribe`` しておく。``MessageBus.subscribe`` は冪等
                # (同じキューを返す) なので ``run()`` 内部の subscribe も
                # 安全に動く。
                for channel in _ROLE_INBOUND_CHANNELS.get(role, ()):
                    bus.subscribe(instance.system_id, channel)

                # REQ 1.5: Run 開始から 5 秒以内に ``system_instantiated``
                # event を発行する。``EventLogWriter.append`` は in-memory
                # queue への enqueue なので 100 ms 以内に完了し、5 秒 SLA
                # は構造的に余裕を持って満たされる。
                await writer.append(
                    "system_instantiated",
                    {
                        "system_id": instance.system_id,
                        "role": role.value,
                        "sub_agent_count": instance.sub_agent_count,
                    },
                    node_id=instance.system_id,
                    actor_id=instance.system_id,
                )

            if resuming_existing:
                await platform.quota_monitor.reconcile()
        except Exception as exc:
            # REQ 1.7: 必須 System の生成失敗は致命的。事後 cleanup を best
            # effort で行ってから ``SystemInstantiationError`` に正規化して
            # raise する。CLI 層は exit code 1 (内部例外) もしくは
            # ``sys.exit(3)`` 相当に翻訳する設計だが、現段階では caller の
            # 判断に委ねる。
            await _cleanup_partial_run(
                writer=writer,
                run_dir=run_dir,
                lockfile_path=lockfile_path,
                workspace_controller=workspace_controller,
            )
            # ``ConfigError`` / ``RunDirectoryError`` などのドメイン例外は
            # そのまま伝播させ、それ以外を ``SystemInstantiationError`` で
            # ラップする。
            if isinstance(exc, (ConfigError, RunDirectoryError, SystemInstantiationError)):
                raise
            raise SystemInstantiationError(
                f"failed to instantiate mandatory Systems for run "
                f"{run_id}: {exc}"
            ) from exc

        return platform

    async def _attach_system_node(
        self,
        system: "System",
        role: SystemRole,
        *,
        parent_id: str | None = None,
        terminable: bool | None = None,
    ) -> None:
        """Register a live System adapter as a persistent Node."""

        node_parent_id = parent_id if parent_id is not None else self._default_parent_for_role(role)
        node_terminable = role is SystemRole.S1_WORKER if terminable is None else terminable
        role_spec = _role_spec_for_system_role(role)
        node = Node(
            id=system.system_id,
            parent_id=node_parent_id,
            vsm_position=role,
            goal=f"{role.value} responsibility",
            terminable=node_terminable,
            differentiation_level=DifferentiationLevel.COLLAPSED,
            source=NodeSource.SPAWN if node_terminable else NodeSource.CONFIG,
            role_spec=role_spec,
            agent_spec={"adapter": system.__class__.__name__},
            status=NodeStatus.CREATED,
        )
        self.nodes[node.id] = node
        run_state = NodeRunState(
            run_id=self.run_id,
            node_id=node.id,
            status=NodeStatus.CREATED,
            budget=self.run_config.budget.envelope_for(role),
            cost_consumed={
                "tokens_in": 0.0,
                "tokens_out": 0.0,
                "tokens_cache_read": 0.0,
                "tokens_total": 0.0,
                "wall_clock_ms": 0.0,
                "node_running_ms": 0.0,
            },
        )
        self.node_run_states[(self.run_id, node.id)] = run_state
        runtime = self.runtimes.get(role)
        explicit_pool = getattr(runtime, "quota_pool", None) if runtime is not None else None
        backend_name = getattr(runtime, "backend_name", None) if runtime is not None else None
        if isinstance(explicit_pool, str) and explicit_pool:
            self._node_pools[node.id] = explicit_pool
        elif backend_name == "claude-code":
            self._node_pools[node.id] = "claude-subscription"
        elif backend_name == "codex":
            self._node_pools[node.id] = "codex-pro"
        system.bind_runtime_control(self)
        system.bind_instruction_queue(
            self.bus.subscribe(system.system_id, ChannelId.INSTRUCTION)
        )
        system.bind_node_context(
            run_id=self.run_id,
            run_state=run_state,
            context_builder=self.context_view_builder,
            resume_within_run=self.run_config.session.resume_within_run,
        )
        if node.parent_id and node.parent_id in self.nodes:
            self.nodes[node.parent_id].child_ids.append(node.id)

        await self.eventlog.append(
            "node_created",
            {
                "node_id": node.id,
                "parent_id": node.parent_id,
                "vsm_position": role.value,
                "terminable": node.terminable,
                "source": node.source.value,
                "differentiation_level": node.differentiation_level.value,
            },
            node_id=node.id,
            actor_id=node.parent_id,
        )

        authority = ParentAuthority(
            authority_id=generate_uuid(),
            issuer_node_id=node.parent_id or node.id,
            subject_node_id=node.id,
            issued_at=datetime.now(timezone.utc),
            may_differentiate_to=DifferentiationLevel.FULL,
            max_depth=4,
            max_spawn_count=self.run_config.s1_dynamic_max,
            budget_envelope=self.run_config.budget.envelope_for(role),
            allowed_tool_classes=frozenset(
                {
                    ToolEffect.PURE_READ,
                    ToolEffect.LOCAL_WRITE,
                    ToolEffect.EXTERNAL_READ,
                    ToolEffect.EXTERNAL_WRITE,
                    ToolEffect.CONTROL,
                    ToolEffect.HUMAN,
                }
            ),
            filesystem_scope=(str(self.run_dir.parent.resolve(strict=False)),),
        )
        self.authorities[authority.authority_id] = authority
        await self.eventlog.append(
            "authority_granted",
            {
                "authority_id": authority.authority_id,
                "issuer_node_id": authority.issuer_node_id,
                "subject_node_id": authority.subject_node_id,
                "may_differentiate_to": authority.may_differentiate_to.value,
                "max_spawn_count": authority.max_spawn_count,
            },
            node_id=node.id,
            actor_id=authority.issuer_node_id,
        )
        await self.eventlog.append(
            "agent_attached",
            {
                "node_id": node.id,
                "agent_kind": system.__class__.__name__,
                "role": role.value,
                "backend": (
                    self.runtimes[role].backend_name
                    if self.runtimes.get(role) is not None
                    else "deterministic"
                ),
                "model": (
                    getattr(self.runtimes[role], "model", "")
                    if self.runtimes.get(role) is not None
                    else ""
                ),
                "budget": dict(run_state.budget),
                "tools": list(role_spec.allowed_tools),
            },
            node_id=node.id,
            actor_id=node.id,
        )

    def _default_parent_for_role(self, role: SystemRole) -> str | None:
        if role is SystemRole.S5_POLICY:
            return None
        if role in {SystemRole.S3_ALLOCATOR, SystemRole.S4_SCANNER}:
            s5 = self.systems.get(SystemRole.S5_POLICY, [])
            return s5[0].system_id if s5 else None
        if role is SystemRole.S3STAR_AUDITOR:
            s3 = self.systems.get(SystemRole.S3_ALLOCATOR, [])
            return s3[0].system_id if s3 else None
        if role in {SystemRole.S2_COORDINATOR, SystemRole.S1_WORKER}:
            s3 = self.systems.get(SystemRole.S3_ALLOCATOR, [])
            return s3[0].system_id if s3 else None
        return None

    async def raise_algedonic(
        self, request: AlgedonicRequest, *, human: bool = False
    ) -> tuple[ToolInvocation, SendResult]:
        """Node または人間から Algedonic signal を S5 へ直送する。"""

        facade = AlgedonicFacade(runner=lambda req, inv: self._run_algedonic(req, inv, human))
        return await facade.raise_algedonic(request)

    async def _run_algedonic(
        self,
        request: AlgedonicRequest,
        invocation: ToolInvocation,
        human: bool,
    ) -> SendResult:
        s5_instances = self.systems.get(SystemRole.S5_POLICY, [])
        if not s5_instances:
            raise RuntimeError("S5 is required for algedonic routing")
        if human:
            sender_role: SystemRole | ExternalRole = ExternalRole.HUMAN
            actor_type = "human"
        else:
            source_node = self.nodes.get(request.source_node_id)
            if source_node is None or not isinstance(source_node.vsm_position, SystemRole):
                raise KeyError(f"unknown source Node: {request.source_node_id}")
            sender_role = source_node.vsm_position
            actor_type = "agent"

        self.tool_invocations[invocation.invocation_id] = invocation
        await self.eventlog.append(
            "tool_invoked",
            {
                "tool_invocation_id": invocation.invocation_id,
                "tool_name": invocation.tool_name,
                "effect": invocation.effect.value,
                "requested_by_node_id": request.source_node_id,
            },
            node_id=None if human else request.source_node_id,
            actor_type=actor_type,
            actor_id=request.source_node_id,
        )
        await self.eventlog.append(
            "algedonic_raised",
            {
                "severity": request.severity,
                "reason": request.reason,
                "source_node_id": request.source_node_id,
                "source_kind": "human" if human else "node",
            },
            node_id=None if human else request.source_node_id,
            actor_type=actor_type,
            actor_id=request.source_node_id,
        )
        if self.run_config.algedonic.notify_human:
            await self.eventlog.append(
                "algedonic_human_notification",
                {
                    "severity": request.severity,
                    "reason": request.reason,
                    "source_node_id": request.source_node_id,
                },
                node_id=None if human else request.source_node_id,
                actor_id=request.source_node_id,
            )
        result = await self.bus.send(
            Message(
                message_id=generate_uuid(),
                sender_role=sender_role,
                sender_id=request.source_node_id,
                receiver_role=SystemRole.S5_POLICY,
                receiver_id=s5_instances[0].system_id,
                channel=ChannelId.ALGEDONIC,
                payload={
                    "severity": request.severity,
                    "reason": request.reason,
                    "source_node_id": request.source_node_id,
                },
                timestamp_ms=int(self.clock.now().timestamp() * 1000),
            )
        )
        if not result.delivered:
            raise RuntimeError("algedonic signal could not be delivered to S5")
        await self.eventlog.append(
            "tool_completed",
            {
                "tool_invocation_id": invocation.invocation_id,
                "tool_name": invocation.tool_name,
                "result": {"delivered": True},
            },
            node_id=None if human else request.source_node_id,
            actor_type=actor_type,
            actor_id=request.source_node_id,
        )
        return result

    async def suspend_node_from_algedonic(
        self, *, source_node_id: str, reason: str, requested_by: str
    ) -> None:
        node = self.nodes.get(source_node_id)
        if node is None:
            raise KeyError(f"unknown source Node: {source_node_id}")
        transition_node_status(
            node,
            self.node_run_states[(self.run_id, node.id)],
            NodeStatus.SUSPENDED,
        )
        await self.eventlog.append(
            "node_suspended",
            {"node_id": node.id, "status": NodeStatus.SUSPENDED.value, "reason": reason},
            node_id=node.id,
            actor_id=requested_by,
        )

    async def convene_consortium(
        self,
        *,
        subject: str,
        convener_node_id: str,
        participant_node_ids: Sequence[str] | None = None,
        trigger: str = "s5",
    ) -> ConsortiumDecision:
        """S5、Algedonic、人間の各トリガから共通合議を開始する。"""

        if participant_node_ids is None:
            root_roles = {
                SystemRole.S3_ALLOCATOR,
                SystemRole.S4_SCANNER,
                SystemRole.S5_POLICY,
            }
            selected = [node for node in self.nodes.values() if node.vsm_position in root_roles]
        else:
            missing = [node_id for node_id in participant_node_ids if node_id not in self.nodes]
            if missing:
                raise KeyError(f"unknown consortium participant Nodes: {missing}")
            selected = [self.nodes[node_id] for node_id in participant_node_ids]
        if convener_node_id not in {node.id for node in selected}:
            convener = self.nodes.get(convener_node_id)
            if convener is None:
                raise KeyError(f"unknown convener Node: {convener_node_id}")
            selected.append(convener)
        participants: list[NodeParticipant] = []
        for node in selected:
            if not isinstance(node.vsm_position, SystemRole):
                raise TypeError(f"Node {node.id} has no SystemRole")
            participants.append(
                NodeParticipant(node=node, runtime=self.runtimes[node.vsm_position])
            )
        return await self.consortium.convene(
            subject=subject,
            participants=participants,
            convener_node_id=convener_node_id,
            trigger=trigger,
        )

    async def convene_consortium_from_human(
        self,
        *,
        subject: str,
        convener_node_id: str,
        participant_node_ids: Sequence[str] | None = None,
    ) -> ConsortiumDecision:
        """Wave 5 API が接続する人間発の公開招集入口。"""

        return await self.convene_consortium(
            subject=subject,
            convener_node_id=convener_node_id,
            participant_node_ids=participant_node_ids,
            trigger="human",
        )

    def submit_consortium_human_statement(
        self, consortium_id: str, statement: str
    ) -> None:
        self.consortium.submit_human_statement(consortium_id, statement)

    def _system_for_node(self, node_id: str) -> "System":
        for systems in self.systems.values():
            for system in systems:
                if system.system_id == node_id:
                    return system
        raise KeyError(f"unknown Node: {node_id}")

    async def deliver_instruction(
        self, instruction: str, *, target_node: str | None = None
    ) -> str:
        """追加指示を記録し、次の LLM invocation 用キューへ配送する。"""

        async with self._shutdown_lock:
            if self._shutdown_task is not None:
                raise RuntimeError("Platform is shutting down")
            return await self._deliver_instruction(
                instruction, target_node=target_node
            )

    async def _deliver_instruction(
        self, instruction: str, *, target_node: str | None
    ) -> str:

        cleaned = instruction.strip()
        if not cleaned:
            raise ValueError("instruction is required")
        if target_node is None:
            s5 = self.systems.get(SystemRole.S5_POLICY, [])
            if not s5:
                raise RuntimeError("S5 Node is not available")
            target = s5[0]
        else:
            target = self._system_for_node(target_node)
        instruction_id = generate_uuid()
        message = Message(
            message_id=generate_uuid(),
            sender_role=ExternalRole.HUMAN,
            sender_id="local-user",
            receiver_role=target.role,
            receiver_id=target.system_id,
            channel=ChannelId.INSTRUCTION,
            payload={
                "instruction_id": instruction_id,
                "instruction": cleaned,
            },
            timestamp_ms=int(self.clock.now().timestamp() * 1000),
        )
        result = await target.deliver_instruction(message, self.bus)
        if not result.delivered:
            raise RuntimeError(f"instruction delivery rejected: {target.system_id}")
        return instruction_id

    async def control_node(self, node_id: str, action: str) -> NodeStatus:
        """Web 介入を Node lifecycle の検証付き遷移として適用する。"""

        node = self.nodes.get(node_id)
        if node is None:
            raise KeyError(f"unknown Node: {node_id}")
        state = self.node_run_states[(self.run_id, node_id)]
        actor_id = "local-user"
        if action == "suspend":
            transition_node_status(node, state, NodeStatus.SUSPENDED)
            self.bus.suspend_receiver(node_id)
            event_type = "node_suspended"
        elif action == "resume":
            transition_node_status(node, state, NodeStatus.RUNNING)
            self.bus.resume_receiver(node_id)
            self._node_started_at[node_id] = self.clock.monotonic()
            event_type = "node_resumed"
        elif action == "terminate":
            if not node.terminable:
                raise ValueError(f"Node is not terminable: {node_id}")
            transition_node_status(node, state, NodeStatus.TERMINATED)
            await self._system_for_node(node_id).shutdown()
            event_type = "node_terminated"
        else:
            raise ValueError(f"unknown Node action: {action}")
        await self.eventlog.append(
            event_type,
            {"node_id": node_id, "status": state.status.value, "reason": "human_control"},
            node_id=node_id,
            actor_type="human",
            actor_id=actor_id,
        )
        return state.status

    async def respond_human_review(self, review_key: str, response: str) -> None:
        cleaned = response.strip()
        if not review_key.strip() or not cleaned:
            raise ValueError("review_key and response are required")
        events = read_all(self.run_dir / _EVENTS_FILENAME)
        requested = any(
            event.get("event_type") == "human_review_requested"
            and (event.get("payload") or {}).get("review_key") == review_key
            for event in events
        )
        already_responded = any(
            event.get("event_type") == "human_review_responded"
            and (event.get("payload") or {}).get("review_key") == review_key
            for event in events
        )
        if not requested or already_responded:
            raise KeyError(f"human review is not pending: {review_key}")
        await self.eventlog.append(
            "human_review_responded",
            {"review_key": review_key, "response": cleaned},
            actor_type="human",
            actor_id="local-user",
        )

    async def before_agent_invoke(self, node_id: str) -> None:
        """単一 invocation の保守見積を Node/Run 残余へ予約できるか検証する。"""

        node = self.nodes[node_id]
        state = self.node_run_states[(self.run_id, node_id)]
        if self.quota_monitor.requires_human_resolution(node_id):
            raise QuotaResolutionRequiredError(
                f"node {node_id} requires human quota resolution"
            )
        if state.status is NodeStatus.SUSPENDED:
            if self.quota_monitor.has_pending_resume(node_id):
                raise QuotaExhaustedError(f"node {node_id} is suspended by quota")
            raise RuntimeError(f"node {node_id} is suspended")

        self._accrue_running_time(node_id)

        node_tokens = state.cost_consumed["tokens_total"]
        node_wall_ms = max(
            state.cost_consumed["wall_clock_ms"],
            state.cost_consumed["node_running_ms"],
        )
        run_tokens = self.run_cost_consumed["tokens_total"]
        run_wall_ms = max(
            self.run_cost_consumed["wall_clock_ms"],
            self.run_cost_consumed["node_running_ms"],
        )
        estimate = self._budget_guard.estimate(node_id)
        remaining = {
            "node_tokens": max(0.0, state.budget["tokens"] - node_tokens),
            "node_wall_clock_seconds": max(
                0.0, state.budget["wall_clock_seconds"] - (node_wall_ms / 1000)
            ),
            "run_tokens": max(
                0.0, self.run_config.budget.run_tokens - run_tokens
            ),
            "run_wall_clock_seconds": max(
                0.0,
                self.run_config.budget.run_wall_clock_seconds
                - (run_wall_ms / 1000),
            ),
        }
        reasons = self._budget_guard.rejection_reasons(
            estimate,
            node_remaining_tokens=remaining["node_tokens"],
            node_remaining_wall_clock_seconds=remaining[
                "node_wall_clock_seconds"
            ],
            run_remaining_tokens=remaining["run_tokens"],
            run_remaining_wall_clock_seconds=remaining[
                "run_wall_clock_seconds"
            ],
        )
        if not reasons:
            return

        reason = ",".join(reasons)
        await self.eventlog.append(
            "budget_exceeded",
            {
                "node_id": node_id,
                "reasons": reasons,
                "invocation_estimate": {
                    "tokens": estimate.tokens,
                    "wall_clock_seconds": estimate.wall_clock_seconds,
                },
                "remaining_before_invocation": remaining,
                "node_consumed": dict(state.cost_consumed),
                "node_budget": dict(state.budget),
                "run_consumed": dict(self.run_cost_consumed),
                "run_budget": {
                    "tokens": self.run_config.budget.run_tokens,
                    "wall_clock_seconds": self.run_config.budget.run_wall_clock_seconds,
                },
            },
            node_id=node_id,
            actor_id=node_id,
        )
        invocation = self._escalations.request_escalation(
            EscalationRequest(
                escalation_key=f"budget:{self.run_id}:{node_id}:{reason}",
                reason="budget_exceeded",
                blocking_issue=reason,
                requested_by=node_id,
                target_authority=node.parent_id or node_id,
            )
        )
        self.tool_invocations[invocation.invocation_id] = invocation
        await self.eventlog.append(
            "escalation_requested",
            dict(invocation.payload),
            node_id=node_id,
            actor_id=node_id,
        )
        raise BudgetExceededError(f"budget exceeded for node {node_id}: {reason}")

    async def after_agent_invoke(
        self,
        node_id: str,
        result: AgentResult,
        pending_message: object | None,
    ) -> None:
        """AgentResult の利用量を累算し、quota 枯渇を休眠へ変換する。"""

        state = self.node_run_states[(self.run_id, node_id)]
        self._accrue_running_time(node_id)
        amounts = {
            "tokens_in": float(result.tokens_in),
            "tokens_out": float(result.tokens_out),
            "tokens_cache_read": float(result.tokens_cache_read),
            "tokens_total": float(
                result.tokens_in + result.tokens_out + result.tokens_cache_read
            ),
            "wall_clock_ms": float(result.latency_ms),
        }
        for key, amount in amounts.items():
            state.cost_consumed[key] += amount
            self.run_cost_consumed[key] += amount
        self._budget_guard.record(
            node_id,
            tokens=int(amounts["tokens_total"]),
            wall_clock_seconds=amounts["wall_clock_ms"] / 1000,
        )
        running_ms = int(state.cost_consumed["node_running_ms"])
        await self.eventlog.append(
            "budget_consumed",
            {
                "node_id": node_id,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "tokens_cache_read": result.tokens_cache_read,
                "wall_clock_ms": result.latency_ms,
                "node_running_ms": running_ms,
                "cumulative": dict(state.cost_consumed),
                "run_cumulative": dict(self.run_cost_consumed),
            },
            node_id=node_id,
            actor_id=node_id,
        )
        if self.metering_hook is not None:
            self.metering_hook(self.run_id, node_id, result)
        if result.quota_exhausted and self.run_config.quota.suspend_on_exhausted:
            message = pending_message if isinstance(pending_message, Message) else None
            reset_at = await self.quota_monitor.suspend(
                node_id, result.quota_reset_at, message, result.quota_kind
            )
            raise QuotaExhaustedError(
                f"quota exhausted for node {node_id}; reset_at={reset_at.isoformat()}"
            )

    def _accrue_running_time(self, node_id: str) -> None:
        started = self._node_started_at.get(node_id)
        if started is None:
            return
        elapsed_ms = max(0.0, (self.clock.monotonic() - started) * 1000)
        if elapsed_ms <= 0:
            return
        state = self.node_run_states[(self.run_id, node_id)]
        state.cost_consumed["node_running_ms"] += elapsed_ms
        self.run_cost_consumed["node_running_ms"] += elapsed_ms
        self._node_started_at[node_id] = self.clock.monotonic()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn each System's ``run()`` coroutine as an asyncio Task.

        :meth:`Platform.create` が必須 System を全て生成し
        ``system_instantiated`` event を append し終えた後に呼ぶ。本
        メソッドは各 System の ``System.start()`` (基底実装で
        ``asyncio.create_task(self.run(), ...)`` を行う) を順に await する
        だけで、System 自体の Task は非同期に走り続ける。

        二重起動は no-op として安全 (基底 ``System.start`` がアイドル
        判定を持つ)。Run の途中で :meth:`spawn_s1` が S1 を追加した
        場合は呼び出し側で個別に ``s1.start()`` する。
        """
        for systems_for_role in self.systems.values():
            for system in systems_for_role:
                await system.start()
                node = self.nodes.get(system.system_id)
                if node is not None and node.status is NodeStatus.CREATED:
                    node.status = NodeStatus.RUNNING
                    self.node_run_states[(self.run_id, node.id)].status = NodeStatus.RUNNING
                    self._node_started_at[node.id] = self.clock.monotonic()
                    await self.eventlog.append(
                        "node_started",
                        {"node_id": node.id, "status": NodeStatus.RUNNING.value},
                        node_id=node.id,
                        actor_id=node.id,
                    )

    async def shutdown(self) -> None:
        """同時呼出しと呼出元 cancel に耐えて shutdown を一度だけ完遂する。"""

        async with self._shutdown_lock:
            if self._shutdown_task is None:
                self._shutdown_task = asyncio.create_task(
                    self._shutdown_once(),
                    name=f"platform-shutdown[{self.run_id}]",
                )
            task = self._shutdown_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # 呼出元の cancel で配下 task 回収や EventLog 排水を中断しない。
            await asyncio.shield(task)
            raise

    async def _shutdown_once(self) -> None:
        """Gracefully tear down all Systems, the EventLogWriter, and lockfile.

        Proposal-owned selfdev worktrees are borrowed by this Platform and
        intentionally remain registered until the selfdev controller reaches
        gate/audit/commit cleanup.

        実行順序:

        1. 全 System の ``run()`` Task を cancel + await (基底実装で
           ``CancelledError`` は握り潰される)。
        2. :class:`EventLogWriter` を ``stop()`` で停止 (sentinel 排水 +
           writer task の終了待機 + ファイルハンドル close)。
        3. ``RUNNING`` lockfile を削除 (REQ 11.6: ``vsm replay`` が
           「Run is no longer active」と判断できるようにする)。

        :class:`OSError` が unlink で起きても無視する: 既に削除された
        / 権限不足 / レース等の状況で本シャットダウンを失敗させる
        必要は無く、Run dir の他のファイルから状態は復元できる。
        """
        await self.quota_monitor.shutdown()

        # 1. Systems の cancel + await。``shutdown`` はサブクラスがオーバ
        # ライドしてもよい (S3*_Auditor の timer task など) ので、基底
        # ``System.shutdown`` を経由する。
        # ``self.systems`` はスナップショット化してから iterate する。
        # ``await system.shutdown()`` 中に S3 が動的 S1 生成を完了しても
        # 孤児化させないよう、未回収 System がなくなるまで再走査する。
        stopped_systems: set[int] = set()
        while True:
            snapshot: list["System"] = [
                system
                for systems_for_role in list(self.systems.values())
                for system in list(systems_for_role)
                if id(system) not in stopped_systems
            ]
            if not snapshot:
                break
            for system in snapshot:
                await system.shutdown()
                stopped_systems.add(id(system))
                node = self.nodes.get(system.system_id)
                if node is not None and node.status is NodeStatus.RUNNING:
                    node.status = NodeStatus.IDLE
                    self.node_run_states[(self.run_id, node.id)].status = NodeStatus.IDLE
                    await self.eventlog.append(
                        "node_idled",
                        {"node_id": node.id, "status": NodeStatus.IDLE.value},
                        node_id=node.id,
                        actor_id=node.id,
                    )

        # CLI セッションは Event_Log から再構築可能な Run 内キャッシュであり、
        # Run 境界を越えて保持しない。
        for run_state in self.node_run_states.values():
            run_state.session_refs.clear()

        workspace_error: Exception | None = None
        # Proposal workspace の所有者は selfdev controller である。Run の
        # Platform は借用者なので、gate/audit/candidate commit までの間に
        # shutdown しても worktree を削除しない。既存の通常 self-hosting
        # Run だけは従来どおり Run 境界で finalize する。
        if self.workspace_controller is not None and (
            self.manifest is None or self.manifest.proposal_id is None
        ):
            try:
                self.workspace_controller.finalize()
            except Exception as exc:
                workspace_error = exc

        # 2. EventLogWriter の停止。これより後の ``append`` 呼び出しは
        # 失敗するが、System が全て停止しているので呼ばれることは無い。
        await self.eventlog.stop()

        # Run 終了時の唯一の LETHE export hook。disabled は完全 no-op、
        # dry-run は共有 JSONL、live は supplemental POST へ出力する。
        lethe_error: Exception | None = None
        if self.run_config.lethe.enabled:
            try:
                await asyncio.to_thread(
                    self.lethe_bridge.export_run,
                    run_id=self.run_id,
                    ended_at=self.clock.now_iso(),
                    events_path=self.run_dir / _EVENTS_FILENAME,
                    nodes=self.nodes,
                    node_run_states=self.node_run_states,
                    run_consumption=self.run_cost_consumed,
                )
            except Exception as exc:
                lethe_error = exc

        # 3. RUNNING lockfile を削除 (REQ 11.6)。Run dir 自体は観測 /
        # replay のため残す (REQ 10.1: Source_of_Truth)。
        lockfile_path = self.run_dir / _RUNNING_LOCKFILE_NAME
        try:
            if lockfile_path.exists():
                lockfile_path.unlink()
        except OSError:
            # best-effort: 削除失敗は Run の状態に影響しない。
            pass

        if workspace_error is not None and lethe_error is not None:
            raise ExceptionGroup(
                "workspace finalize and LETHE export both failed",
                [workspace_error, lethe_error],
            )
        if workspace_error is not None:
            raise workspace_error
        if lethe_error is not None:
            raise lethe_error

    # ------------------------------------------------------------------
    # Dynamic S1 spawn
    # ------------------------------------------------------------------

    async def spawn_s1(
        self,
        *,
        specialization: str,
        initial_assignment: dict[str, Any] | str,
    ) -> "System":
        """Dynamically create and start a new :class:`S1Worker`.

        S3_Allocator が directive を解釈した結果として「新しい
        specialization の S1 が必要」と判断した場合に呼ぶ (REQ 7.3, 13.6)。
        本メソッドは以下の不変条件を強制する:

        - **REQ 13.6**: 動的生成 S1 の総数は ``run_config.s1_dynamic_max``
          (既定 64) を超えない。
        - **REQ 1.3**: 全 S1 の総数は ``run_config.s1_max`` (既定 1024) を
          超えない。

        いずれかを超えると :class:`SystemInstantiationError` を raise する
        (S3_Allocator 側で捕捉して ``s1_instantiation_error`` event を
        append し、5 秒以内に S5 へ通知する責務 — REQ 7.5)。

        Parameters
        ----------
        specialization : str
            S1 の専門化ラベル (例: ``"frontend"``, ``"test"``,
            ``"backend"``)。S2_Coordinator の conflict 検出と
            S3_Allocator の reuse 判定 (idle = 同 specialization かつ
            current_assignments 空) に使われる (REQ 7.2)。
        initial_assignment : dict | str
            S1 が起動直後に取り組む最初の作業項目。``s1_instantiated``
            event の payload にそのまま転写される (REQ 7.4)。schema 上は
            string を想定しているため、dict が渡された場合は呼び出し側で
            JSON 化 / 文字列化する責務を持つ。

        Returns
        -------
        System
            生成済み S1Worker (``run()`` Task は開始済み)。

        Raises
        ------
        SystemInstantiationError
            ``s1_dynamic_max`` (REQ 13.6) または ``s1_max`` (REQ 1.3) を
            超過した場合。
        """
        # REQ 13.6: 動的生成 S1 の concurrent 上限。``s1_max`` より小さい
        # こと (RunConfig 構築時に検証済み) なので、こちらを先にチェック
        # する。
        if self._s1_count >= self.run_config.s1_dynamic_max:
            raise SystemInstantiationError(
                f"S1 dynamic concurrent ceiling reached "
                f"(s1_dynamic_max={self.run_config.s1_dynamic_max}, "
                f"REQ 13.6); cannot spawn more S1_Workers"
            )

        # REQ 1.3: 全 S1 の絶対上限。動的生成 + 起動時生成 (現状 0 想定) の
        # 合計で評価する設計だが、PoC では起動時 S1 数 = 0 のため
        # ``_s1_count`` のみを判定すれば十分。
        if self._s1_count >= self.run_config.s1_max:
            raise SystemInstantiationError(
                f"S1 absolute ceiling reached "
                f"(s1_max={self.run_config.s1_max}, REQ 1.3); "
                "cannot spawn more S1_Workers"
            )

        requested_by = (
            self.systems.get(SystemRole.S3_ALLOCATOR, [None])[0].system_id
            if self.systems.get(SystemRole.S3_ALLOCATOR)
            else self.run_id
        )
        spawn_key = f"{self.run_id}:spawn_s1:{specialization}:{self._s1_count + 1}"
        facade = SpawnChildFacade(runner=self._run_spawn_child)
        request = SpawnChildRequest(
            spawn_key=spawn_key,
            requested_by=requested_by,
            specialization=specialization,
            initial_assignment=initial_assignment,
        )
        authority = ParentAuthority(
            authority_id=f"{self.run_id}:spawn_child",
            issuer_node_id=requested_by,
            subject_node_id=requested_by,
            issued_at=datetime.now(timezone.utc),
            max_spawn_count=self.run_config.s1_dynamic_max,
            allowed_tool_classes=frozenset({ToolEffect.CONTROL}),
        )
        _, result = await facade.spawn_child(request, authority)
        return self.systems[SystemRole.S1_WORKER][-1]

    async def _run_spawn_child(
        self,
        request: SpawnChildRequest,
        invocation: ToolInvocation,
    ) -> SpawnChildResult:
        """Execute the concrete S1Worker spawn behind ``spawn_child``."""
        self.tool_invocations[invocation.invocation_id] = invocation
        await self.eventlog.append(
            "tool_invoked",
            {
                "tool_invocation_id": invocation.invocation_id,
                "tool_name": invocation.tool_name,
                "effect": invocation.effect.value,
                "idempotency_key": invocation.idempotency_key,
                "requested_by_node_id": invocation.requested_by_node_id,
            },
            node_id=request.requested_by,
            actor_id=request.requested_by,
        )

        # 遅延 import: ``S1Worker`` は Task 17.1 で実装される。
        from vsm.systems.s1_worker import S1Worker  # type: ignore[import-not-found]

        s1: System = S1Worker(  # type: ignore[call-arg]
            system_id=generate_uuid(),
            eventlog=self.eventlog,
            bus=self.bus,
            runtime=self.runtimes[SystemRole.S1_WORKER],
            clock=self.clock,
            platform=self,
            run_config=self.run_config,
            specialization=request.specialization,
        )

        # Defensive: S1Worker.__init__ が Sub_Agent を登録しなかった場合は
        # 1 個だけ "default" を入れる。Task 17.1 の実装は本コメントに従って
        # 自身で ``run_config.count(SystemRole.S1_WORKER)`` 個を登録すべき。
        if s1.sub_agent_count == 0:
            s1.register_sub_agent(label="default")
        s1.bind_workdir(self.workdir)

        # 状態カウンタを更新してから event を append する (event 中の
        # 集計と一致させるため)。
        self.systems.setdefault(SystemRole.S1_WORKER, []).append(s1)
        self._s1_count += 1
        await self._attach_system_node(
            s1,
            SystemRole.S1_WORKER,
            parent_id=request.requested_by,
            terminable=True,
        )

        # REQ 7.4: ``s1_instantiated`` を 1 秒以内に append。``initial_assignment``
        # は schema 上 ``str`` のため、dict が来たら ``str(...)`` で
        # シリアライズする (本格的な JSON 化は呼び出し側に委ねる)。
        await self.eventlog.append(
            "s1_instantiated",
            {
                "s1_id": s1.system_id,
                "specialization": request.specialization,
                "initial_assignment": (
                    request.initial_assignment
                    if isinstance(request.initial_assignment, str)
                    else str(request.initial_assignment)
                ),
            },
            node_id=s1.system_id,
            actor_id=request.requested_by,
        )

        # REQ 1.6: 動的生成された S1 も ``system_instantiated`` を発行する
        # (REQ 1.5 と対称な観測性)。``s1_instantiated`` と二重発行する
        # ことで、replay 時に S1 の生成タイミングと specialization の
        # 双方を一意に追跡できる。
        await self.eventlog.append(
            "system_instantiated",
            {
                "system_id": s1.system_id,
                "role": SystemRole.S1_WORKER.value,
                "sub_agent_count": s1.sub_agent_count,
            },
            node_id=s1.system_id,
            actor_id=s1.system_id,
        )

        # **REQ 7.6 の隠れた前提**: S3_Allocator は ``spawn_s1`` の return 直後に
        # ``S1_S3`` で assignment を送る。S1 の ``run()`` ループが
        # ``subscribe`` を呼ぶより先に send が走ると、
        # :class:`MessageBus.send` は受信者キュー未登録として
        # ``channel_rejected`` を返してしまう (バス側の構造的な
        # rejection 経路)。それを避けるため、``s1.start()`` の前に
        # S1 が購読する 3 チャネルすべてを事前 ``subscribe`` する。
        # ``MessageBus.subscribe`` は同 ``(receiver_id, channel)`` への
        # 二度目の呼び出しに対しても同一キューを返すため、``run()``
        # 内部で行われる ``subscribe`` も冪等で動く。
        self.bus.subscribe(s1.system_id, ChannelId.S1_S3)
        self.bus.subscribe(s1.system_id, ChannelId.S1_S2)
        self.bus.subscribe(s1.system_id, ChannelId.S3STAR_TO_S1)

        # 起動も lifecycle 内で完結させる (S3_Allocator は ``await
        # platform.spawn_s1(...)`` の戻り値を ``current_assignments`` に
        # 追加するだけで済む)。
        await s1.start()
        node = self.nodes.get(s1.system_id)
        if node is not None:
            node.status = NodeStatus.RUNNING
            self.node_run_states[(self.run_id, node.id)].status = NodeStatus.RUNNING
            self._node_started_at[node.id] = self.clock.monotonic()
            await self.eventlog.append(
                "node_started",
                {"node_id": node.id, "status": NodeStatus.RUNNING.value},
                node_id=node.id,
                actor_id=node.id,
            )
        await self.eventlog.append(
            "tool_completed",
            {
                "tool_invocation_id": invocation.invocation_id,
                "tool_name": invocation.tool_name,
                "result": {"node_id": s1.system_id},
            },
            node_id=s1.system_id,
            actor_id=s1.system_id,
        )
        return SpawnChildResult(node_id=s1.system_id)

    @property
    def s1_count(self) -> int:
        """Current count of dynamically-spawned S1_Worker instances.

        Property 11 (Bounded counts) のテストや S3_Allocator の状態確認で
        使う read-only プロパティ。``shutdown`` 後は値が保たれる
        (Run dir の Event_Log を Source_of_Truth とするため)。
        """
        return self._s1_count


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _resolve_role_runtimes(
    *,
    run_config: RunConfig,
    llm_config: LLMConfig,
    llm_override: LLMProviderProtocol | None,
    runtime_overrides: Mapping[SystemRole, AgentRuntimeProtocol | None] | None,
    process_factory: Callable[..., Any] | None = None,
) -> dict[SystemRole, AgentRuntimeProtocol | None]:
    """設定からロールごとに独立した runtime インスタンスを構築する。"""

    configured_roles = tuple(SystemRole)
    if runtime_overrides is not None:
        missing = [role.value for role in configured_roles if role not in runtime_overrides]
        if missing:
            raise ConfigError(
                missing_roles=missing,
                detail="runtime_overrides は全 SystemRole を明示する必要があります",
            )
        invalid = [
            role.value
            for role in configured_roles
            if (run_config.agents.backend_for(role) is None)
            != (runtime_overrides[role] is None)
        ]
        if invalid:
            raise ConfigError(
                missing_roles=invalid,
                detail="runtime_overrides が [agents.roles] の割り当てと一致しません",
            )
        return {role: runtime_overrides[role] for role in configured_roles}

    if llm_override is not None:
        from vsm.llm.fake import FakeLLMProvider

        result: dict[SystemRole, AgentRuntimeProtocol | None] = {}
        for role in configured_roles:
            if run_config.agents.backend_for(role) is None:
                result[role] = None
            elif isinstance(llm_override, FakeLLMProvider):
                result[role] = FakeAgentRuntime(provider=llm_override)
            else:
                result[role] = LiteLLMRuntimeAdapter(provider=llm_override)
        return result

    result = {}
    configuration_errors: list[tuple[str, str]] = []
    litellm_configuration_error: str | None = None
    for role in configured_roles:
        backend_name = run_config.agents.backend_for(role)
        if backend_name is None:
            result[role] = None
            continue
        backend = run_config.agents.backends[backend_name]
        if backend_name == "claude-code":
            if backend.bin is None:
                configuration_errors.append((role.value, "claude-code bin is required"))
                continue
            kwargs: dict[str, Any] = {
                "claude_bin": backend.bin,
                "model": backend.model,
                "timeout_seconds": backend.timeout_seconds,
            }
            if process_factory is not None:
                kwargs["process_factory"] = process_factory
            result[role] = ClaudeCodeRuntime(
                **kwargs,
            )
        elif backend_name == "codex":
            if backend.bin is None or backend.reasoning_effort is None:
                configuration_errors.append(
                    (role.value, "codex bin and reasoning_effort are required")
                )
                continue
            kwargs = {
                "codex_bin": backend.bin,
                "model": backend.model,
                "reasoning_effort": backend.reasoning_effort,
                "timeout_seconds": backend.timeout_seconds,
            }
            if process_factory is not None:
                kwargs["process_factory"] = process_factory
            result[role] = CodexRuntime(
                **kwargs,
            )
        elif backend_name == "litellm":
            from vsm.llm.provider import LLMProvider

            if litellm_configuration_error is not None:
                configuration_errors.append(
                    (role.value, litellm_configuration_error)
                )
                continue
            try:
                provider = LLMProvider(llm_config)
            except ConfigError as exc:
                litellm_configuration_error = exc.detail
                configuration_errors.append((role.value, exc.detail))
                continue
            result[role] = LiteLLMRuntimeAdapter(
                provider=provider,
                timeout_seconds=backend.timeout_seconds,
                model=provider.default_model,
            )
        elif backend_name == "fake":
            result[role] = FakeAgentRuntime(
                model=backend.model,
                timeout_seconds=backend.timeout_seconds,
            )
        else:
            configuration_errors.append(
                (role.value, f"unsupported agent backend: {backend_name}")
            )
    if configuration_errors:
        raise ConfigError(
            missing_roles=[role for role, _detail in configuration_errors],
            detail=configuration_errors[0][1],
        )
    return result


def describe_role_runtimes(
    *,
    run_config: RunConfig,
    llm_config: LLMConfig,
    runtime_overrides: Mapping[SystemRole, AgentRuntimeProtocol | None] | None = None,
) -> list[dict[str, str]]:
    """Run で選択されるロール別 backend/model を構築して返す。

    Web 層が独自のフォールバックを持たず、``start_run`` と同じ
    ``_resolve_role_runtimes`` を事前検証にも利用するための公開投影である。
    runtime の構築に失敗した場合は、そのまま ``ConfigError`` を返す。
    """

    runtimes = _resolve_role_runtimes(
        run_config=run_config,
        llm_config=llm_config,
        llm_override=None,
        runtime_overrides=runtime_overrides,
    )
    return [
        {
            "role": role.value,
            "backend": (
                runtime.backend_name
                if runtime is not None
                else "deterministic"
            ),
            "model": (
                str(getattr(runtime, "model", ""))
                if runtime is not None
                else ""
            ),
        }
        for role in SystemRole
        for runtime in (runtimes[role],)
    ]


async def start_run(
    *,
    run_id: str | None = None,
    runs_dir: Path = _DEFAULT_RUNS_DIR,
    run_config: RunConfig | None = None,
    manifest: RunManifest | None = None,
    llm_config: LLMConfig | None = None,
    llm_override: LLMProviderProtocol | None = None,
    runtime_overrides: Mapping[SystemRole, AgentRuntimeProtocol | None] | None = None,
    clock: Clock | None = None,
    context_view_hook: ContextViewHook | None = None,
    human_statement_waiter: HumanStatementWaiter | None = None,
    lethe_transport: LetheTransport | None = None,
    lethe_context_query: str | None = None,
    lethe_context_injector: LetheContextInjector | None = None,
    metering_hook: Callable[[str, str, AgentResult], Any] | None = None,
    resume: bool = False,
) -> Platform:
    """Top-level coroutine to start a new Run.

    CLI ``vsm submit`` から呼ばれる本 PoC のメインエントリポイント。
    内部では :meth:`Platform.create` で構造検証 / Run dir 作成 /
    ``system_instantiated`` event 発行を行い、続いて
    :meth:`Platform.start` で各 System の asyncio Task を起動する。

    Parameters
    ----------
    run_id : str | None
        Run 識別子。``None`` のとき :func:`vsm.ids.generate_run_id` で
        新規発行する (REQ 4.6)。
    runs_dir : Path
        Run ディレクトリ群の親 (既定 ``Path("runs")``).
    run_config : RunConfig | None
        Run 構造設定。``None`` で既定値。
    llm_config : LLMConfig | None
        LLM プロバイダー設定。``llm_override`` 指定時は無視される。
    llm_override : LLMProviderProtocol | None
        テストでプロバイダーを直接差し込みたい場合に使用。
    clock : Clock | None
        ``None`` で :class:`SystemClock`、テストでは :class:`FakeClock`。

    Returns
    -------
    Platform
        起動済み Platform (全必須 System の Task が走っている状態)。
        終了時は呼び出し側が ``await platform.shutdown()`` を呼ぶ。

    Raises
    ------
    ConfigError
        必須 System が不足している (REQ 13.1〜13.3)。CLI が exit code 3
        に翻訳する。
    RunDirectoryError
        Run ディレクトリ作成に失敗した (REQ 10.4)。CLI が exit code 4 に
        翻訳する。
    SystemInstantiationError
        必須 System のインスタンス化に失敗した (REQ 1.7)。

    Validates Requirements: 1.5, 10.3, 10.4, 13.1, 13.2, 13.3.
    """
    rid = run_id if run_id is not None else generate_run_id()
    platform = await Platform.create(
        run_id=rid,
        runs_dir=runs_dir,
        run_config=run_config,
        manifest=manifest,
        llm_config=llm_config,
        llm_override=llm_override,
        runtime_overrides=runtime_overrides,
        clock=clock,
        context_view_hook=context_view_hook,
        human_statement_waiter=human_statement_waiter,
        lethe_transport=lethe_transport,
        lethe_context_query=lethe_context_query,
        lethe_context_injector=lethe_context_injector,
        metering_hook=metering_hook,
        resume=resume,
    )
    await platform.start()
    return platform


async def _cleanup_partial_run(
    *,
    writer: EventLogWriter,
    run_dir: Path,
    lockfile_path: Path,
    workspace_controller: WorkspaceController | None = None,
) -> None:
    """Best-effort cleanup when :meth:`Platform.create` fails partway.

    ``EventLogWriter`` 起動後 / 必須 System 全数生成前に例外が起きた場合に
    呼ばれる。writer task を停止し、``RUNNING`` lockfile と (空ファイル
    または部分書き込みの) ``events.jsonl`` / Run dir を削除する。
    どの操作も失敗しても残りを継続する (cleanup の失敗は元例外を覆い
    隠さない)。

    Note
    ----
    本関数は :meth:`Platform.shutdown` と異なり Systems を await しない
    (生成途中で List に入りきっていない可能性があるため)。生成済みで
    list に入った System は asyncio Task をまだ起動していない (start
    は :meth:`Platform.start` で初めて呼ばれる) ので cancel 不要。
    """
    # 1. writer 停止 (cancel + close)。
    try:
        await writer.stop()
    except Exception:
        # 停止すらできない場合でも、後段の dir 削除は試みる。
        pass

    if workspace_controller is not None and (
        workspace_controller.manifest.proposal_id is None
    ):
        try:
            workspace_controller.discard()
        except Exception:
            pass

    # 2. lockfile 削除。
    try:
        if lockfile_path.exists():
            lockfile_path.unlink()
    except OSError:
        pass

    # 3. events.jsonl 削除。
    try:
        events_path = run_dir / _EVENTS_FILENAME
        if events_path.exists():
            events_path.unlink()
    except OSError:
        pass

    for partial_path in (run_dir / "manifest.json", run_dir / "workspace"):
        try:
            if partial_path.is_file():
                partial_path.unlink()
            elif partial_path.is_dir():
                partial_path.rmdir()
        except OSError:
            pass

    # 4. Run dir 自体を削除 (空のはず)。``rmdir`` は中身があると失敗する
    # が、Sub_Agent や Systems がディスクを触る経路は無いので空のはず。
    try:
        if run_dir.exists():
            run_dir.rmdir()
    except OSError:
        # 残置は許容: 後続の ``mkdir(exist_ok=False)`` で衝突した場合は
        # Caller が別 run_id を使えば良い。
        pass
