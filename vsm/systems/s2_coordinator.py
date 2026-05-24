"""S2_Coordinator: VSM System 2 (Task 15.1).

design.md `## Components and Interfaces` §S2_Coordinator
(``vsm/systems/s2_coordinator.py``) と Sequence Diagrams §S2_Coordinator
conflict 解消 に対応する実装。S2_Coordinator は **複数の S1_Worker が
並行実行されるとき** にそれらの相互干渉を調整する役割を担う
(:doc:`requirements.md` §Requirement 8 全体)。

Responsibilities
----------------
- **REQ 8.1**: 2 つ以上の S1_Worker が並行して assignment を実行している
  間、``S1_S2`` Channel で coordination request と conflict signal を
  監視する。
- **REQ 8.2**: 同一 ``specialization`` の S1_Worker が同一 ``work_item_id``
  を同時に保持していたら conflict として認識する。本ロジックは pure
  function :func:`detect_conflict` として切り出し、Property 9 の PBT
  (``tests/property/test_s2_conflict_detection.py``) で検証可能にする。
- **REQ 8.3**: conflict 検出から coordination directive 生成まで 5 秒
  以内。本実装ではスキャンサイクル内で同期的に検出 → 生成を行うため、
  実時間ではミリ秒オーダで完了する。
- **REQ 8.4**: directive を全ての該当 S1_Worker に ``S1_S2`` で 1 秒
  以内に配送する。:class:`MessageBus.send` は同一 event-loop tick 内
  でキュー投入が完了するため、構造的に SLA を満たす。
- **REQ 8.5** *(参考、S1 側責務)*: directive を受領した S1_Worker は 1
  秒以内に ack を ``S1_S2`` 経由で返す。S2 側はその ack を観測する
  だけで、S1 の応答自体は :class:`S1Worker` (Task 17.1) が担う。
- **REQ 8.6**: directive 配送から 30 秒経過しても ack が届かない場合、
  ``coordination_ack_missing`` event を ``directive_id`` / ``s1_id`` /
  ``elapsed_ms`` 付きで append する。
- **REQ 8.7**: ``coordination_conflict``、``coordination_directive``、
  ``coordination_ack`` の各 event を 1 秒以内に Event_Log に append
  する。Event_Log writer は in-memory queue へのハンドオフであるため、
  append は常に sub-millisecond で完了し、構造的に 1 秒 SLA を満たす。

Design notes
------------
* **conflict scan の駆動源は Platform 上の生 S1 状態**: REQ 8.2 の判定
  対象は「実時点で各 S1_Worker が保持している ``current_assignments``」
  であり、メッセージ履歴ではない。よって S2 は ``platform.systems``
  経由で各 :class:`S1Worker` の ``specialization`` / ``current_assignments``
  属性を直接読み出して conflict を検出する。これにより、conflict 検出
  は S1_S2 チャネルのメッセージ取りこぼしに依存しない (REQ 8.1 の
  "monitor" 要件は、S2 が ack 等のメッセージを受け取りつつスキャンを
  行う組み合わせで満たされる)。
* **ack 受信は S1_S2 チャネル経由**: S1 側 (Task 17.1) は directive 受領
  後 1 秒以内に ``payload={"type": "ack", "directive_id": ...}`` を
  ``S1_S2`` で返す。本実装では ``payload["type"]`` を ``"ack"`` で
  ディスパッチし、``coordination_ack`` event を append する。それ
  以外の payload (status / 自由記述等) は無視する。
* **既知 conflict のデデュープ**: 同一 ``(specialization, work_item_id)``
  の conflict が連続スキャンで再検出されても directive を二重発行
  しないよう、``_known_conflicts`` set で抑制する。新しい work_item
  / specialization 組み合わせのみが directive を生む。
* **ack timeout 検出**: ``_pending_acks`` は ``directive_id`` →
  ``(remaining_s1_ids, deadline_monotonic)`` のマップ。S1 から ack を
  受領するたびに ``remaining_s1_ids`` から該当 S1 を取り除き、空に
  なったらエントリを削除する。30 秒経過時点で ``remaining`` に残って
  いる S1 についてのみ ``coordination_ack_missing`` を append する。

Validates Requirements: 8.1, 8.2, 8.3, 8.4, 8.6, 8.7.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
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
    # 循環 import 回避のための遅延型参照。Platform は本モジュールを
    # 実体として import する側 (lifecycle 経由) なので、実行時の参照は
    # ``self._platform`` 経由でアトリビュートアクセスのみとする。
    from vsm.runtime.lifecycle import Platform


__all__ = ["Conflict", "detect_conflict", "S2Coordinator"]


# REQ 8.6: 30 秒経過しても ack が無い場合に ``coordination_ack_missing``
# を append する。tests (Property 4) は ``FakeClock`` でこの値を境界
# として検証する。
_ACK_TIMEOUT_SECONDS: float = 30.0

# conflict 検出の poll 周期 (秒)。run() ループは ``asyncio.wait`` に
# 本値を timeout として渡し、メッセージ受信もしくは周期到達のどちら
# でもスキャンを 1 サイクル実行する。
#
# - REQ 8.7 (1 秒 SLA) 達成の観点: ``coordination_ack`` は S1_S2 で
#   メッセージ到着即時にディスパッチされる (周期に依存しない) ため、
#   本値は ack の SLA に影響しない。
# - REQ 8.4 (directive 1 秒配送) 達成の観点: directive 配送は conflict
#   検出と同じサイクル内で送出されるため、検出後の配送は単一 event-loop
#   tick で完了する。
_CONFLICT_DETECTION_INTERVAL: float = 1.0


@dataclass(frozen=True)
class Conflict:
    """同一 specialization の S1 群が同一 work_item を保持している事象。

    design.md §Data Models §Conflict / Coordination Directive と同一の
    形状。``s1_ids`` は :class:`tuple` (``frozen=True`` 互換) で、
    REQ 8.2 の不変条件 ``|s1_ids| >= 2`` を満たす。

    Attributes
    ----------
    specialization : str
        衝突した S1 群の専門化ラベル (例: ``"frontend"``)。
    work_item_id : str
        2 つ以上の S1 が同時に保持している作業項目 ID。
    s1_ids : tuple[str, ...]
        衝突に巻き込まれた S1 の ``system_id`` 一覧 (重複なし)。
    """

    specialization: str
    work_item_id: str
    s1_ids: tuple[str, ...]


def detect_conflict(s1_states: dict[str, dict[str, Any]]) -> list[Conflict]:
    """Pure function realising REQ 8.2's conflict definition.

    *Property 9* (`tests/property/test_s2_conflict_detection.py`) が検証
    する仕様:

        expected(S) := { (spec, wi) | |{ s ∈ S :
            s.specialization == spec ∧ wi ∈ s.current_assignments }| ≥ 2 }

    本関数は ``expected(S)`` と完全一致する ``(specialization,
    work_item_id)`` 射影を持つ :class:`Conflict` リストを返す。``s1_ids``
    には該当する全ての S1 ID が含まれる。

    Parameters
    ----------
    s1_states : dict[str, dict]
        ``s1_id`` → ``{"specialization": str, "current_assignments":
        list[str]}`` のマップ。S2 が ``Platform.systems`` から都度組み
        立てる (本関数自体は I/O を持たない)。

    Returns
    -------
    list[Conflict]
        REQ 8.2 を満たす全ての ``(specialization, work_item_id)`` 衝突
        の集合。``|s1_ids| >= 2`` を持つもののみが含まれる。

    Validates Requirements: 8.2.
    """
    # ``(specialization, work_item_id)`` をキーにして、各組み合わせを
    # 主張する s1_id を収集する。``defaultdict(list)`` で重複削除は
    # しないが、入力 ``s1_states`` が辞書 (キーが s1_id) であるため、
    # 同一 s1_id が同一 work_item を二重カウントすることは無い。
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s1_id, state in s1_states.items():
        spec = state.get("specialization", "")
        for wi in state.get("current_assignments", []):
            groups[(spec, wi)].append(s1_id)

    # REQ 8.2: |s1_ids| >= 2 の組み合わせのみを Conflict として返す。
    # ``tuple(v)`` で frozen dataclass の hashable 化要件を満たす。
    return [
        Conflict(
            specialization=spec,
            work_item_id=wi,
            s1_ids=tuple(s1_ids),
        )
        for (spec, wi), s1_ids in groups.items()
        if len(s1_ids) >= 2
    ]


class S2Coordinator(System):
    """VSM System 2 — coordination across concurrent S1_Worker instances.

    Lifecycle 層 (:class:`vsm.runtime.lifecycle.Platform`) が Run start 時
    に 1 つだけインスタンス化する。``run()`` は ``shutdown`` までの間、
    S1_S2 チャネルのドレインと conflict 検出 / ack timeout チェックを
    並行に行う単一ループとなる。

    Validates Requirements: 8.1〜8.7.
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
            role=SystemRole.S2_COORDINATOR,
            eventlog=eventlog,
            llm=llm,
            clock=clock,
        )
        # Bus / Platform / RunConfig は ``run()`` ループで都度参照する
        # ため、コンストラクタ時点で属性として保持する。``platform``
        # は前方参照 (TYPE_CHECKING) で型付けし、循環 import を避ける。
        self._bus: MessageBus = bus
        self._platform: "Platform" = platform
        self._run_config: RunConfig = run_config

        # REQ 1.4 / Lifecycle 層の Sub_Agent 登録契約: ``run_config.count``
        # が 0 を返す病理ケースでも最低 1 個の Sub_Agent を確保する。
        # Lifecycle の defensive 登録 (``"default"`` ラベル) より早く本
        # ループで登録することで、``system_instantiated`` event の
        # ``sub_agent_count`` payload が正しい値で発行される。
        for i in range(max(1, run_config.count(SystemRole.S2_COORDINATOR))):
            self.register_sub_agent(label=f"coordinator-{i}")

        # ``directive_id`` → ``(remaining_s1_ids, deadline_monotonic)``。
        # REQ 8.6 の ack timeout 検出用。``_check_ack_timeouts`` が周期
        # 的に走査し、deadline 経過後に ``coordination_ack_missing`` を
        # append する。
        self._pending_acks: dict[str, tuple[list[str], float]] = {}

        # 既に directive を発行済みの ``(specialization, work_item_id)``
        # 組み合わせを記録するデデュープセット。同一 conflict が複数
        # サイクルにまたがって観測されても directive を二重発行しない。
        self._known_conflicts: set[tuple[str, str]] = set()

    async def run(self) -> None:
        """Main S2_Coordinator loop.

        各サイクルで以下を実行する:

        1. **S1_S2 メッセージのドレイン**: ``asyncio.wait`` を
           :data:`_CONFLICT_DETECTION_INTERVAL` 秒の timeout で呼び、
           タイムアウト前に到着したメッセージを処理する。複数メッセージ
           がキューに溜まっている場合は同サイクル内で全て drain する
           ことで REQ 8.7 (ack append 1 秒 SLA) を結構な余裕で満たす。
        2. **conflict スキャン + directive 配信** (REQ 8.1〜8.4, 8.7):
           :func:`detect_conflict` を呼び、新規 conflict があれば即座に
           directive を生成 / 配送し、event を append する。
        3. **ack timeout チェック** (REQ 8.6): ``_pending_acks`` を
           走査し、deadline 経過分について ``coordination_ack_missing``
           を append する。

        ``shutdown`` で Task が cancel されると ``CancelledError`` が
        伝播し、上位の ``System.shutdown`` がそれを握り潰して正常終了
        させる (基底クラスの動作)。
        """
        # 自分宛の S1_S2 受信キューを取得。``MessageBus.subscribe`` は
        # 既存キューがあればそれを返すため、再起動シナリオでも
        # in-flight メッセージを取りこぼさない。
        q_s1_s2 = self._bus.subscribe(self.system_id, ChannelId.S1_S2)

        while True:
            # --------------------------------------------------------------
            # 1. S1_S2 ドレイン (REQ 8.1, 8.7 — ack 受信側)
            # --------------------------------------------------------------
            # 周期 (1 秒) を timeout として ``queue.get`` を待つ。message が
            # 到着していたら処理し、続いて非ブロッキングで残りを drain
            # する。これにより 1 サイクル内に到着した全メッセージが
            # 同サイクル内で append まで完了するため、REQ 8.7 の 1 秒
            # SLA を構造的に満たす。
            drain_task = asyncio.create_task(q_s1_s2.get(), name="s2_s1_s2_get")
            done, _ = await asyncio.wait(
                {drain_task},
                timeout=_CONFLICT_DETECTION_INTERVAL,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if drain_task in done:
                first_msg: Message = drain_task.result()
                await self._handle_s1_s2_message(first_msg)
                # 残りの保留メッセージを非ブロッキングで処理する。
                # ``QueueEmpty`` 受信時点で停止し、次の周期に進む。
                while True:
                    try:
                        msg = q_s1_s2.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await self._handle_s1_s2_message(msg)
            else:
                # メッセージが届かないままタイムアウトした場合は
                # ``drain_task`` を cancel してリソースを解放する。
                # ``queue.get`` の cancel はメッセージ消費を起こさない
                # ので、次サイクルで再生成しても取りこぼしは無い。
                drain_task.cancel()

            # --------------------------------------------------------------
            # 2. conflict scan + directive 配信 (REQ 8.1〜8.4, 8.7)
            # --------------------------------------------------------------
            await self._scan_and_dispatch_conflicts()

            # --------------------------------------------------------------
            # 3. ack timeout チェック (REQ 8.6)
            # --------------------------------------------------------------
            await self._check_ack_timeouts()

    async def _handle_s1_s2_message(self, msg: Message) -> None:
        """Dispatch a single S1_S2 message.

        現状認識する payload type は ``"ack"`` のみ (S1 側が directive
        受領後に返送する)。それ以外の status / 自由記述 payload は
        REQ 8.1 の "monitor" 要件にぶら下がる将来拡張用途のため、本
        実装では silently 無視する (Event_Log の ``channel_message``
        として既に :class:`MessageBus` 側で append 済みである点に注意)。

        Parameters
        ----------
        msg : Message
            S1_S2 で到着したメッセージ。``msg.sender_id`` を ack 元 S1
            の識別子として用いる。

        Validates Requirements: 8.7 (ack append).
        """
        payload = msg.payload
        if not isinstance(payload, dict):
            # 想定外 payload は無視。``MessageBus`` が既に
            # ``channel_message`` で生 payload を記録している。
            return
        if payload.get("type") != "ack":
            return

        directive_id = payload.get("directive_id", "")
        s1_id = msg.sender_id

        # REQ 8.7: ``coordination_ack`` を 1 秒以内に append。
        # ``EventLogWriter.append`` は in-memory queue への hand-off で
        # ms オーダで完了するため、構造的に SLA を満たす。
        await self._eventlog.append(
            "coordination_ack",
            {"directive_id": directive_id, "s1_id": s1_id},
        )

        # ack timeout の追跡から該当 S1 を除外する。これによりその後
        # ``_check_ack_timeouts`` が当該 S1 について
        # ``coordination_ack_missing`` を発行することは無くなる。
        if directive_id in self._pending_acks:
            s1_ids, deadline = self._pending_acks[directive_id]
            remaining = [x for x in s1_ids if x != s1_id]
            if remaining:
                self._pending_acks[directive_id] = (remaining, deadline)
            else:
                # 全 S1 が ack したのでエントリを削除。
                del self._pending_acks[directive_id]

    async def _scan_and_dispatch_conflicts(self) -> None:
        """Detect new conflicts and dispatch directives.

        ``Platform.systems[S1_WORKER]`` から各 S1 の現在状態を読み出し、
        :func:`detect_conflict` で純粋関数的に conflict を検出する。
        既知の conflict (``_known_conflicts``) は再発行を避け、新規分
        について以下を 1 サイクル内に同期実行する:

        1. ``coordination_conflict`` を append (REQ 8.7)
        2. directive を生成 (REQ 8.3 — 5 秒以内、構造的に ms オーダ)
        3. ``coordination_directive`` を append (REQ 8.7)
        4. 各 S1 へ ``S1_S2`` で directive を送出 (REQ 8.4 — 1 秒以内)
        5. ``_pending_acks`` に登録して ack timeout 監視を開始 (REQ 8.6)

        Validates Requirements: 8.1, 8.2, 8.3, 8.4, 8.7.
        """
        # ``platform.systems[S1_WORKER]`` 配下の各 S1 から live state を
        # 集める。S1Worker (Task 17.1) は ``specialization`` と
        # ``current_assignments`` 属性を公開する契約。実装が揃う前の
        # 暫定では ``getattr(..., default)`` で安全側にフォールバック
        # する (空リスト → conflict 候補に上がらない)。
        s1_states: dict[str, dict[str, Any]] = {}
        for s1 in self._platform.systems.get(SystemRole.S1_WORKER, []):
            s1_states[s1.system_id] = {
                "specialization": getattr(s1, "specialization", ""),
                "current_assignments": list(
                    getattr(s1, "current_assignments", [])
                ),
            }

        # REQ 8.2: pure function による conflict 集合の同定。
        conflicts = detect_conflict(s1_states)

        for conflict in conflicts:
            key = (conflict.specialization, conflict.work_item_id)
            if key in self._known_conflicts:
                # 既に directive を発行済み。S1 側が directive を適用する
                # まで current_assignments が変化しないため、同じ conflict
                # が複数サイクルにわたって観測されることは想定範囲内。
                continue
            self._known_conflicts.add(key)

            # ----------------------------------------------------------
            # REQ 8.7: coordination_conflict を append (1 秒 SLA)。
            # schema 制約: s1_ids は min_length=2 (REQ 8.2 の不変条件)。
            # ----------------------------------------------------------
            await self._eventlog.append(
                "coordination_conflict",
                {
                    "specialization": conflict.specialization,
                    "work_item_id": conflict.work_item_id,
                    "s1_ids": list(conflict.s1_ids),
                },
            )

            # ----------------------------------------------------------
            # REQ 8.3: directive を 5 秒以内に生成。本実装は同期的に
            # 一意の directive_id を発行し、最初の S1 を primary とする
            # シンプルな yield ポリシーを採用する (将来 LLM Sub_Agent
            # で拡張可能)。
            # ----------------------------------------------------------
            directive_id = generate_uuid()
            primary_s1_id = conflict.s1_ids[0]
            directive_text = (
                f"yield to first; only {primary_s1_id} continues with "
                f"{conflict.work_item_id}"
            )

            # REQ 8.7: coordination_directive を append (1 秒 SLA)。
            # schema 制約: affected_s1_ids は min_length=1。
            await self._eventlog.append(
                "coordination_directive",
                {
                    "directive_id": directive_id,
                    "affected_s1_ids": list(conflict.s1_ids),
                    "directive": directive_text,
                },
            )

            # ----------------------------------------------------------
            # REQ 8.4: 1 秒以内に全該当 S1 に S1_S2 で directive を配送。
            # ``MessageBus.send`` は単一 event-loop tick 内でキュー投入
            # を完了するため、構造的に SLA を満たす。``return_exceptions``
            # を使った gather も検討したが、本コードは単純なシリアル
            # 配送で十分 (S1 数は最大 1024 / 通常は 1 桁、各 send は
            # in-memory queue への push)。
            # ----------------------------------------------------------
            for s1_id in conflict.s1_ids:
                await self._bus.send(
                    Message(
                        message_id=generate_uuid(),
                        sender_role=SystemRole.S2_COORDINATOR,
                        sender_id=self.system_id,
                        receiver_role=SystemRole.S1_WORKER,
                        receiver_id=s1_id,
                        channel=ChannelId.S1_S2,
                        payload={
                            "type": "directive",
                            "directive_id": directive_id,
                            "directive": directive_text,
                        },
                        timestamp_ms=int(self._clock.monotonic() * 1000),
                    )
                )

            # ----------------------------------------------------------
            # REQ 8.6: ack timeout 監視を 30 秒の deadline で開始する。
            # ``_check_ack_timeouts`` が次以降のサイクルで監視する。
            # ----------------------------------------------------------
            self._pending_acks[directive_id] = (
                list(conflict.s1_ids),
                self._clock.monotonic() + _ACK_TIMEOUT_SECONDS,
            )

    async def _check_ack_timeouts(self) -> None:
        """Detect missing acks and append ``coordination_ack_missing``.

        REQ 8.6: directive 配送から 30 秒以内に ack が無かった S1 に
        ついて、``directive_id`` / ``s1_id`` / ``elapsed_ms`` を含む
        ``coordination_ack_missing`` event を append する。timeout した
        directive は ``_pending_acks`` から取り除き、二重発行を避ける。

        ``elapsed_ms`` は schema (``ge=0``) 制約に従い、整数化した値を
        渡す。本実装ではちょうど ``_ACK_TIMEOUT_SECONDS`` 経過時点で
        発火するため、当該定数の ms 値を採用する (実時間ではポーリング
        間隔の分わずかに超過するが、SLA 違反ではない)。

        Validates Requirements: 8.6.
        """
        now = self._clock.monotonic()

        # イテレーション中に dict を破壊するため、まず expired を抽出して
        # から削除 + append を行う。
        expired: list[tuple[str, list[str]]] = []
        for directive_id, (s1_ids, deadline) in list(self._pending_acks.items()):
            if now >= deadline:
                expired.append((directive_id, s1_ids))
                # ``del`` を先に行うことで、ループの最後で同じ directive_id
                # に対する重複 append が起きないことを担保する。
                del self._pending_acks[directive_id]

        elapsed_ms = int(_ACK_TIMEOUT_SECONDS * 1000)
        for directive_id, s1_ids in expired:
            # 残っている (= ack を返さなかった) S1 全てについて event を
            # 一件ずつ発行する。schema は ``s1_id`` が単一文字列のため、
            # 1 directive × N missing ack で N event 発行となる。
            for s1_id in s1_ids:
                await self._eventlog.append(
                    "coordination_ack_missing",
                    {
                        "directive_id": directive_id,
                        "s1_id": s1_id,
                        "elapsed_ms": elapsed_ms,
                    },
                )
