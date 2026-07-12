"""AgentRuntime quota 枯渇時の Node 休眠・自動復帰。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone

from vsm.clock import Clock, format_iso_ms
from vsm.eventlog.writer import EventLogWriter
from vsm.messaging.bus import MessageBus
from vsm.messaging.message import Message
from vsm.nodes import Node, NodeRunState, NodeStatus, transition_node_status

Sleep = Callable[[float], Awaitable[None]]


class QuotaMonitor:
    """Node 単位の quota timer と Message 保留キューを所有する。"""

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
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._eventlog = eventlog
        self._bus = bus
        self._clock = clock
        self._nodes = nodes
        self._states = node_run_states
        self._run_id = run_id
        self._fallback = timedelta(minutes=fallback_resume_minutes)
        self._sleep = sleep
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    async def suspend(
        self,
        node_id: str,
        reset_at: datetime | None,
        pending_message: Message | None = None,
    ) -> datetime:
        if self._closed:
            raise RuntimeError("QuotaMonitor is shut down")
        node = self._nodes[node_id]
        state = self._states[(self._run_id, node_id)]
        now = self._clock.now()
        if reset_at is None:
            reset_at = now + self._fallback
        elif reset_at.tzinfo is None:
            raise ValueError("quota_reset_at must be timezone-aware")
        reset_at = reset_at.astimezone(timezone.utc)

        transition_node_status(node, state, NodeStatus.SUSPENDED)
        self._bus.suspend_receiver(node_id)
        if pending_message is not None:
            self._bus.defer(pending_message)
        await self._eventlog.append(
            "quota_exhausted",
            {"node_id": node_id, "reset_at": format_iso_ms(reset_at)},
            node_id=node_id,
            actor_id=node_id,
        )

        prior = self._timers.pop(node_id, None)
        if prior is not None and not prior.done():
            prior.cancel()
        self._timers[node_id] = asyncio.create_task(
            self._resume_at(node_id, reset_at), name=f"quota-resume[{node_id}]"
        )
        return reset_at

    async def _resume_at(self, node_id: str, reset_at: datetime) -> None:
        try:
            delay = max(0.0, (reset_at - self._clock.now()).total_seconds())
            await self._sleep(delay)
            await self.resume(node_id, reset_at)
        except asyncio.CancelledError:
            raise

    async def resume(self, node_id: str, reset_at: datetime | None = None) -> None:
        if self._closed:
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
        current = asyncio.current_task()
        timer = self._timers.get(node_id)
        if timer is current or timer is None or timer.done():
            self._timers.pop(node_id, None)

    async def shutdown(self) -> None:
        self._closed = True
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            if not timer.done():
                timer.cancel()
        if timers:
            await asyncio.gather(*timers, return_exceptions=True)

    @property
    def timer_count(self) -> int:
        return sum(not task.done() for task in self._timers.values())

    def has_pending_resume(self, node_id: str) -> bool:
        """当該 Node の quota 復帰 timer をこの monitor が所有するか返す。"""

        timer = self._timers.get(node_id)
        return timer is not None and not timer.done()
