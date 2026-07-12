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
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vsm.agents.backends import (
    ClaudeCodeRuntime,
    CodexRuntime,
    FakeAgentRuntime,
    LiteLLMRuntimeAdapter,
)
from vsm.agents.runtime import AgentRuntimeProtocol
from vsm.authority import ParentAuthority
from vsm.clock import Clock, SystemClock
from vsm.config import LLMConfig, RunConfig
from vsm.errors import ConfigError, RunDirectoryError, SystemInstantiationError
from vsm.eventlog.writer import EventLogWriter
from vsm.ids import generate_run_id, generate_uuid
from vsm.llm.types import LLMProviderProtocol
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId
from vsm.nodes import DifferentiationLevel, Node, NodeRunState, NodeSource, NodeStatus
from vsm.roles import MANDATORY_ROLES, RoleSpec, SystemRole
from vsm.tools import (
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
    ) -> None:
        self.run_id: str = run_id
        self.run_dir: Path = run_dir
        self.eventlog: EventLogWriter = eventlog
        self.bus: MessageBus = bus
        self.runtimes = dict(runtimes)
        self.clock: Clock = clock
        self.run_config: RunConfig = run_config

        # 役割別 System インスタンス。``MANDATORY_ROLES`` 各役割に対して
        # 1 つずつ、S1_WORKER については :meth:`spawn_s1` が動的に追加。
        self.systems: dict[SystemRole, list[System]] = {}

        # Refactor 20260608: Systems remain the execution adapter, while Node
        # owns persistent responsibility/history/authority.
        self.nodes: dict[str, Node] = {}
        self.node_run_states: dict[tuple[str, str], NodeRunState] = {}
        self.authorities: dict[str, ParentAuthority] = {}
        self.tool_invocations: dict[str, ToolInvocation] = {}

        # 動的 S1 生成回数のカウンタ。``s1_dynamic_max`` (REQ 13.6) と
        # ``s1_max`` (REQ 1.3) の双方を :meth:`spawn_s1` が確認する。
        # ``current`` ではなく **総生成数** をカウントする (REQ 13.6 が
        # 「concurrent instances」を要求する一方、PoC では terminate 時に
        # decrement する経路がまだないため、現実的に総量で代用する。
        # 後続 task で terminate 経路が追加されたら同期的に decrement
        # する設計にすること)。
        self._s1_count: int = 0

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
        llm_config: LLMConfig | None = None,
        llm_override: LLMProviderProtocol | None = None,
        runtime_overrides: Mapping[SystemRole, AgentRuntimeProtocol | None] | None = None,
        clock: Clock | None = None,
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
        run_dir = runs_dir / run_id
        events_path = run_dir / _EVENTS_FILENAME
        lockfile_path = run_dir / _RUNNING_LOCKFILE_NAME
        try:
            # REQ 10.3: parents=True で ``runs/`` 自体が無くても作れる。
            # exist_ok=False で「既存 Run の上書き」を防ぐ。同名 Run を
            # 観測する場合は CLI ``vsm replay`` / ``vsm status`` を使う。
            run_dir.mkdir(parents=True, exist_ok=False)
            # ``events.jsonl`` を空ファイルとして先に作っておく。
            # ``EventLogWriter`` は ``"a"`` モードで open するので
            # touch 不要だが、CLI ``vsm tail`` などが Run start 直後に
            # ファイル存在確認をできるよう先に作る (REQ 11.7 の対称)。
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

        # writer は他の依存より先に start し、後段でエラーが起きても
        # ``_cleanup_partial_run`` が確実に stop できるようにする。
        writer = EventLogWriter(run_id=run_id, path=events_path, clock=clk)
        await writer.start()

        # MessageBus は writer に依存する (channel_message / channel_rejected
        # を append するため)。
        bus = MessageBus(eventlog=writer)

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
        )

        try:
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
                instance: System = system_cls(  # type: ignore[call-arg]
                    system_id=generate_uuid(),
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
        self.node_run_states[(self.run_id, node.id)] = NodeRunState(
            run_id=self.run_id,
            node_id=node.id,
            status=NodeStatus.CREATED,
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
                    await self.eventlog.append(
                        "node_started",
                        {"node_id": node.id, "status": NodeStatus.RUNNING.value},
                        node_id=node.id,
                        actor_id=node.id,
                    )

    async def shutdown(self) -> None:
        """Gracefully tear down all Systems, the EventLogWriter, and lockfile.

        実行順序:

        1. 全 System の ``run()`` Task を cancel + await (基底実装で
           ``CancelledError`` は握り潰される)。
        2. :class:`EventLogWriter` を ``stop()`` で停止 (writer task の
           cancel + ファイルハンドル close)。
        3. ``RUNNING`` lockfile を削除 (REQ 11.6: ``vsm replay`` が
           「Run is no longer active」と判断できるようにする)。

        :class:`OSError` が unlink で起きても無視する: 既に削除された
        / 権限不足 / レース等の状況で本シャットダウンを失敗させる
        必要は無く、Run dir の他のファイルから状態は復元できる。
        """
        # 1. Systems の cancel + await。``shutdown`` はサブクラスがオーバ
        # ライドしてもよい (S3*_Auditor の timer task など) ので、基底
        # ``System.shutdown`` を経由する。
        # ``self.systems`` はスナップショット化してから iterate する:
        # ``await system.shutdown()`` 中に他の System (典型的には S3) が
        # まだ動的 S1 生成 (spawn_s1) を起こしうるため、辞書 / リスト
        # 双方を直接 iterate すると ``RuntimeError: dictionary changed
        # size during iteration`` が起きる。スナップショットを取れば、
        # 後から追加された System は次の Run で参照される (Run 自体は
        # ここで終了するので問題ない)。
        snapshot: list["System"] = [
            system
            for systems_for_role in list(self.systems.values())
            for system in list(systems_for_role)
        ]
        for system in snapshot:
            await system.shutdown()
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

        # 2. EventLogWriter の停止。これより後の ``append`` 呼び出しは
        # 失敗するが、System が全て停止しているので呼ばれることは無い。
        await self.eventlog.stop()

        # 3. RUNNING lockfile を削除 (REQ 11.6)。Run dir 自体は観測 /
        # replay のため残す (REQ 10.1: Source_of_Truth)。
        lockfile_path = self.run_dir / _RUNNING_LOCKFILE_NAME
        try:
            if lockfile_path.exists():
                lockfile_path.unlink()
        except OSError:
            # best-effort: 削除失敗は Run の状態に影響しない。
            pass

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
    for role in configured_roles:
        backend_name = run_config.agents.backend_for(role)
        if backend_name is None:
            result[role] = None
            continue
        backend = run_config.agents.backends[backend_name]
        if backend_name == "claude-code":
            if backend.bin is None:
                raise ConfigError(missing_roles=[role.value], detail="claude-code bin is required")
            result[role] = ClaudeCodeRuntime(
                claude_bin=backend.bin,
                model=backend.model,
                timeout_seconds=backend.timeout_seconds,
            )
        elif backend_name == "codex":
            if backend.bin is None or backend.reasoning_effort is None:
                raise ConfigError(
                    missing_roles=[role.value],
                    detail="codex bin and reasoning_effort are required",
                )
            result[role] = CodexRuntime(
                codex_bin=backend.bin,
                model=backend.model,
                reasoning_effort=backend.reasoning_effort,
                timeout_seconds=backend.timeout_seconds,
            )
        elif backend_name == "litellm":
            from vsm.llm.provider import LLMProvider

            result[role] = LiteLLMRuntimeAdapter(
                provider=LLMProvider(llm_config),
                timeout_seconds=backend.timeout_seconds,
            )
        elif backend_name == "fake":
            result[role] = FakeAgentRuntime(
                model=backend.model,
                timeout_seconds=backend.timeout_seconds,
            )
        else:
            raise ConfigError(
                missing_roles=[role.value], detail=f"unsupported agent backend: {backend_name}"
            )
    return result


async def start_run(
    *,
    run_id: str | None = None,
    runs_dir: Path = _DEFAULT_RUNS_DIR,
    run_config: RunConfig | None = None,
    llm_config: LLMConfig | None = None,
    llm_override: LLMProviderProtocol | None = None,
    runtime_overrides: Mapping[SystemRole, AgentRuntimeProtocol | None] | None = None,
    clock: Clock | None = None,
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
        llm_config=llm_config,
        llm_override=llm_override,
        runtime_overrides=runtime_overrides,
        clock=clock,
    )
    await platform.start()
    return platform


async def _cleanup_partial_run(
    *,
    writer: EventLogWriter,
    run_dir: Path,
    lockfile_path: Path,
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

    # 4. Run dir 自体を削除 (空のはず)。``rmdir`` は中身があると失敗する
    # が、Sub_Agent や Systems がディスクを触る経路は無いので空のはず。
    try:
        if run_dir.exists():
            run_dir.rmdir()
    except OSError:
        # 残置は許容: 後続の ``mkdir(exist_ok=False)`` で衝突した場合は
        # Caller が別 run_id を使えば良い。
        pass
