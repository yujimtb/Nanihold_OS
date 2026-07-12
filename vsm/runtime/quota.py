"""Durable quota pool circuit breaker と quota 待機中の Message 保持。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vsm.clock import Clock, format_iso_ms
from vsm.eventlog.writer import EventLogWriter
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId, ExternalRole
from vsm.messaging.message import Message
from vsm.nodes import Node, NodeRunState, NodeStatus, transition_node_status
from vsm.roles import SystemRole

Sleep = Callable[[float], Awaitable[None]]
Probe = Callable[[str], Awaitable[bool]]
NodeHook = Callable[[str], None]


@dataclass
class _PoolState:
    pool: str
    quota_kind: str
    reset_at: datetime
    node_ids: list[str]


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("persisted quota reset_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _role_from_value(value: str) -> SystemRole | ExternalRole:
    try:
        return SystemRole(value)
    except ValueError:
        return ExternalRole(value)


class QuotaMonitor:
    """認証 pool 単位の circuit breaker と durable resume reconciler。"""

    def __init__(
        self,
        *,
        eventlog: EventLogWriter,
        bus: MessageBus,
        clock: Clock,
        nodes: Mapping[str, Node],
        node_run_states: Mapping[tuple[str, str], NodeRunState],
        run_id: str,
        fallback_resume_minutes: float,
        weekly_fallback_resume_minutes: float | None = None,
        node_pools: Mapping[str, str] | None = None,
        state_path: Path | None = None,
        probe: Probe | None = None,
        on_node_suspended: NodeHook | None = None,
        on_node_resumed: NodeHook | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._eventlog = eventlog
        self._bus = bus
        self._clock = clock
        self._nodes = nodes
        self._states = node_run_states
        self._run_id = run_id
        self._fallback = timedelta(minutes=fallback_resume_minutes)
        self._weekly_fallback = timedelta(
            minutes=(
                weekly_fallback_resume_minutes
                if weekly_fallback_resume_minutes is not None
                else fallback_resume_minutes
            )
        )
        self._node_pools = node_pools if node_pools is not None else {}
        self._state_path = state_path
        self._probe = probe
        self._on_node_suspended = on_node_suspended
        self._on_node_resumed = on_node_resumed
        self._sleep = sleep
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._pools: dict[str, _PoolState] = {}
        self._closed = False
        self._load_state()
        self._bus.set_deferred_callback(self._persist)

    def _pool_for_node(self, node_id: str) -> str:
        # pool を持たない deterministic runtime は node 単位で隔離する。
        return self._node_pools.get(node_id, f"node:{node_id}")

    def _fallback_for(self, quota_kind: str) -> timedelta:
        return self._weekly_fallback if quota_kind == "weekly" else self._fallback

    def _normalise_reset_at(
        self, reset_at: datetime | None, quota_kind: str
    ) -> datetime:
        if reset_at is None:
            # 既存の Wave 2 runtime は provider が quota 種別を返せない
            # synthetic test/runtime だけ ``unknown`` を使う。正式な
            # selfdev resume 接続面で使う five_hour/weekly は reset 不明を
            # 推測せず fail-fast する。
            if quota_kind == "unknown":
                return self._clock.now() + self._fallback_for(quota_kind)
            raise ValueError("quota_reset_at が不明なため QUOTA_WAIT を開始できません")
        if reset_at.tzinfo is None:
            raise ValueError("quota_reset_at must be timezone-aware")
        return reset_at.astimezone(timezone.utc)

    async def suspend(
        self,
        node_id: str,
        reset_at: datetime | None,
        pending_message: Message | None = None,
        quota_kind: str = "unknown",
    ) -> datetime:
        """枯渇を pool 全体へ伝播し、1 pool につき1 timerを作る。"""

        if self._closed:
            raise RuntimeError("QuotaMonitor is shut down")
        if quota_kind not in {"five_hour", "weekly", "unknown"}:
            raise ValueError(f"unknown quota kind: {quota_kind}")
        if node_id not in self._nodes:
            raise KeyError(f"unknown quota node: {node_id}")
        pool = self._pool_for_node(node_id)
        state = self._states[(self._run_id, node_id)]
        # 既に人間操作などで休止している Node は二重遷移を許さない。
        if self._nodes[node_id].status is NodeStatus.QUOTA_WAIT:
            transition_node_status(self._nodes[node_id], state, NodeStatus.QUOTA_WAIT)

        reset = self._normalise_reset_at(reset_at, quota_kind)
        current = self._pools.get(pool)
        if current is not None:
            # 同一 pool の後続検知は、早い reset を真実として保つ。
            if reset < current.reset_at:
                current.reset_at = reset
                current.quota_kind = quota_kind
                self._persist()
                self._schedule(pool)
            if pending_message is not None:
                self._bus.defer(pending_message)
            return current.reset_at

        node_ids = [
            candidate_id
            for candidate_id in sorted(self._nodes)
            if self._pool_for_node(candidate_id) == pool
            and self._nodes[candidate_id].status
            not in {NodeStatus.TERMINATED, NodeStatus.COMPLETED, NodeStatus.FAILED}
        ]
        if node_id not in node_ids:
            node_ids.append(node_id)
        node_ids = sorted(set(node_ids))

        if self._on_node_suspended is not None:
            for candidate_id in node_ids:
                self._on_node_suspended(candidate_id)
        for candidate_id in node_ids:
            candidate = self._nodes[candidate_id]
            candidate_state = self._states[(self._run_id, candidate_id)]
            if candidate.status is not NodeStatus.QUOTA_WAIT:
                transition_node_status(candidate, candidate_state, NodeStatus.QUOTA_WAIT)
                self._bus.suspend_receiver(candidate_id)
        if pending_message is not None:
            self._bus.defer(pending_message)

        self._pools[pool] = _PoolState(
            pool=pool,
            quota_kind=quota_kind,
            reset_at=reset,
            node_ids=node_ids,
        )
        await self._eventlog.append(
            "quota_exhausted",
            {
                "node_id": node_id,
                "pool": pool,
                "quota_kind": quota_kind,
                "reset_at": format_iso_ms(reset),
            },
            node_id=node_id,
            actor_id=node_id,
        )
        await self._eventlog.append(
            "quota_pool_opened",
            {
                "pool": pool,
                "quota_kind": quota_kind,
                "reset_at": format_iso_ms(reset),
                "node_ids": node_ids,
                "trigger_node_id": node_id,
            },
            node_id=node_id,
            actor_id=node_id,
        )
        self._persist()
        self._schedule(pool)
        return reset

    def _schedule(self, pool: str) -> None:
        prior = self._timers.get(pool)
        current = asyncio.current_task()
        if prior is not None and prior is not current and not prior.done():
            prior.cancel()
        state = self._pools[pool]
        self._timers[pool] = asyncio.create_task(
            self._resume_at(pool, state.reset_at), name=f"quota-probe[{pool}]"
        )

    async def _resume_at(self, pool: str, reset_at: datetime) -> None:
        try:
            delay = max(0.0, (reset_at - self._clock.now()).total_seconds())
            await self._sleep(delay)
            await self._probe_and_resume(pool)
        except asyncio.CancelledError:
            raise

    async def _probe_and_resume(self, pool: str) -> None:
        state = self._pools.get(pool)
        if state is None or self._closed:
            return
        healthy = True
        if self._probe is not None:
            try:
                healthy = await self._probe(pool)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                healthy = False
                await self._eventlog.append(
                    "quota_probe_failed",
                    {"pool": pool, "reason": str(exc)},
                    actor_id="quota-monitor",
                )
        if not healthy:
            self._persist()
            await self._eventlog.append(
                "quota_probe_failed",
                {
                    "pool": pool,
                    "reason": "health probe returned quota unavailable",
                },
                actor_id="quota-monitor",
            )
            return

        node_ids = list(state.node_ids)
        await self._eventlog.append(
            "quota_pool_closed",
            {
                "pool": pool,
                "quota_kind": state.quota_kind,
                "reset_at": format_iso_ms(state.reset_at),
                "node_ids": node_ids,
            },
            actor_id="quota-monitor",
        )
        # resume は probe 成功後に一つずつ行い、各 Message queue の再投入も
        # 完了してから次の Node を進める。
        for node_id in node_ids:
            node = self._nodes.get(node_id)
            if node is None or node.status is not NodeStatus.QUOTA_WAIT:
                continue
            run_state = self._states[(self._run_id, node_id)]
            if self._on_node_resumed is not None:
                self._on_node_resumed(node_id)
            transition_node_status(node, run_state, NodeStatus.RUNNING)
            pending_count = self._bus.resume_receiver(node_id)
            await self._eventlog.append(
                "quota_resumed",
                {
                    "node_id": node_id,
                    "pool": pool,
                    "quota_kind": state.quota_kind,
                    "reset_at": format_iso_ms(state.reset_at),
                    "pending_messages_requeued": pending_count,
                },
                node_id=node_id,
                actor_id=node_id,
            )
            await self._sleep(0)
        self._pools.pop(pool, None)
        self._persist()
        timer = self._timers.get(pool)
        if timer is not None and timer is not asyncio.current_task() and not timer.done():
            timer.cancel()
        self._timers.pop(pool, None)

    async def resume(self, node_id: str, reset_at: datetime | None = None) -> None:
        """明示 resume。pool が開いていれば pool 全体を probe して復帰する。"""

        if self._closed:
            return
        pool = self._pool_for_node(node_id)
        if pool in self._pools:
            if reset_at is not None:
                if reset_at.tzinfo is None:
                    raise ValueError("quota_reset_at must be timezone-aware")
                self._pools[pool].reset_at = reset_at.astimezone(timezone.utc)
            await self._probe_and_resume(pool)
            return
        node = self._nodes[node_id]
        state = self._states[(self._run_id, node_id)]
        transition_node_status(node, state, NodeStatus.RUNNING)
        pending_count = self._bus.resume_receiver(node_id)
        await self._eventlog.append(
            "quota_resumed",
            {
                "node_id": node_id,
                "reset_at": format_iso_ms(reset_at or self._clock.now()),
                "pending_messages_requeued": pending_count,
            },
            node_id=node_id,
            actor_id=node_id,
        )

    async def reconcile(self) -> None:
        """起動後に disk の pool 状態を現在の Node へ適用する。"""

        if self._closed:
            raise RuntimeError("QuotaMonitor is shut down")
        for pool, state in list(self._pools.items()):
            resolved_ids = [
                node_id
                for node_id in state.node_ids
                if node_id in self._nodes
            ]
            # Persisted state is authoritative, but a newly reconstructed static
            # node can be discovered by its pool when the old process did not
            # flush its node list between two events.
            resolved_ids.extend(
                node_id
                for node_id in self._nodes
                if self._pool_for_node(node_id) == pool
            )
            state.node_ids = sorted(set(resolved_ids))
            for node_id in state.node_ids:
                node = self._nodes[node_id]
                run_state = self._states[(self._run_id, node_id)]
                if node.status is not NodeStatus.QUOTA_WAIT:
                    transition_node_status(node, run_state, NodeStatus.QUOTA_WAIT)
                self._bus.suspend_receiver(node_id)
            self._schedule(pool)
        self._persist()
        if self._pools:
            await self._eventlog.append(
                "quota_state_reconciled",
                {
                    "pools": sorted(self._pools),
                    "node_ids": sorted(
                        node_id
                        for state in self._pools.values()
                        for node_id in state.node_ids
                    ),
                },
                actor_id="quota-monitor",
            )

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("quota-state.json の root は object でなければなりません")
        if payload.get("version") != 1:
            raise ValueError("quota-state.json の version は1固定です")
        if payload.get("run_id") != self._run_id:
            raise ValueError("quota-state.json の run_id が現在の Run と一致しません")
        pools = payload.get("pools")
        if not isinstance(pools, list):
            raise ValueError("quota-state.json の pools は配列でなければなりません")
        for item in pools:
            if not isinstance(item, dict):
                raise ValueError("quota-state.json の pool state が不正です")
            required = {"pool", "quota_kind", "reset_at", "node_ids"}
            if set(item) != required:
                raise ValueError("quota-state.json の pool state field が不正です")
            if not isinstance(item["pool"], str) or not item["pool"]:
                raise ValueError("quota-state.json の pool が不正です")
            if item["quota_kind"] not in {"five_hour", "weekly", "unknown"}:
                raise ValueError("quota-state.json の quota_kind が不正です")
            if not isinstance(item["node_ids"], list) or any(
                not isinstance(value, str) or not value for value in item["node_ids"]
            ):
                raise ValueError("quota-state.json の node_ids が不正です")
            state = _PoolState(
                pool=item["pool"],
                quota_kind=item["quota_kind"],
                reset_at=_parse_datetime(item["reset_at"]),
                node_ids=list(item["node_ids"]),
            )
            if len(state.node_ids) != len(set(state.node_ids)):
                raise ValueError("quota-state.json の node_ids は unique でなければなりません")
            self._pools[state.pool] = state
        messages = payload.get("pending_messages", [])
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
            raise ValueError("quota-state.json の pending_messages が不正です")
        self._bus.restore_deferred(
            [self._message_from_payload(item) for item in messages]
        )

    def _persist(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "run_id": self._run_id,
            "pools": [
                {
                    "pool": state.pool,
                    "quota_kind": state.quota_kind,
                    "reset_at": format_iso_ms(state.reset_at),
                    "node_ids": list(state.node_ids),
                }
                for state in sorted(self._pools.values(), key=lambda item: item.pool)
            ],
            "pending_messages": [
                self._message_to_payload(message)
                for message in self._bus.deferred_messages()
            ],
        }
        temporary = self._state_path.with_suffix(".tmp")
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self._state_path)

    @staticmethod
    def _message_to_payload(message: Message) -> dict[str, Any]:
        return {
            "message_id": message.message_id,
            "sender_role": message.sender_role.value,
            "sender_id": message.sender_id,
            "receiver_role": message.receiver_role.value,
            "receiver_id": message.receiver_id,
            "channel": message.channel.value,
            "payload": message.payload,
            "timestamp_ms": message.timestamp_ms,
        }

    @staticmethod
    def _message_from_payload(payload: Mapping[str, Any]) -> Message:
        return Message(
            message_id=str(payload["message_id"]),
            sender_role=_role_from_value(str(payload["sender_role"])),
            sender_id=str(payload["sender_id"]),
            receiver_role=SystemRole(str(payload["receiver_role"])),
            receiver_id=str(payload["receiver_id"]),
            channel=ChannelId(str(payload["channel"])),
            payload=dict(payload["payload"]),
            timestamp_ms=int(payload["timestamp_ms"]),
        )

    async def shutdown(self) -> None:
        self._closed = True
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            if not timer.done():
                timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        self._persist()

    @property
    def timer_count(self) -> int:
        return sum(not task.done() for task in self._timers.values())

    def has_pending_resume(self, node_id: str) -> bool:
        """当該 Node が属する pool の resume timer を所有するか返す。"""

        return self._pool_for_node(node_id) in self._pools and (
            self._timers.get(self._pool_for_node(node_id)) is not None
            and not self._timers[self._pool_for_node(node_id)].done()
        )

    @property
    def pool_states(self) -> dict[str, dict[str, Any]]:
        return {
            pool: {
                "quota_kind": state.quota_kind,
                "reset_at": state.reset_at,
                "node_ids": list(state.node_ids),
            }
            for pool, state in self._pools.items()
        }
