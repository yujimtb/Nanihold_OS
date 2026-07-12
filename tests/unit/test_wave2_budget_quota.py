from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from vsm.agents.backends.fake import FakeAgentRuntime
from vsm.agents.runtime import AgentResult
from vsm.clock import FakeClock
from vsm.config import AgentsConfig, BudgetConfig, RunConfig, load_config
from vsm.errors import BudgetExceededError, QuotaExhaustedError
from vsm.eventlog.reader import read_all
from vsm.eventlog.writer import EventLogWriter
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId
from vsm.messaging.message import Message
from vsm.nodes import Node, NodeRunState, NodeStatus
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform
from vsm.runtime.quota import QuotaMonitor


def _fake_run_config(*, budget: BudgetConfig | None = None) -> RunConfig:
    roles = {role: "fake" for role in SystemRole}
    roles[SystemRole.S3_ALLOCATOR] = ""
    return RunConfig(
        agents=AgentsConfig(default_backend="fake", roles=roles),
        budget=budget or BudgetConfig(),
    )


def test_budget_and_quota_toml_are_loaded_strictly(tmp_path) -> None:
    path = tmp_path / "vsm.toml"
    path.write_text(
        """
[budget]
run_tokens = 1234
run_wall_clock_seconds = 56
[budget.roles]
S5_POLICY = { tokens = 100, wall_clock_seconds = 7 }
[quota]
suspend_on_exhausted = true
fallback_resume_minutes = 3
weekly_fallback_resume_minutes = 30
""",
        encoding="utf-8",
    )

    _, config = load_config(path)

    assert config.budget.run_tokens == 1234
    assert config.budget.envelope_for(SystemRole.S5_POLICY) == {
        "tokens": 100.0,
        "wall_clock_seconds": 7.0,
    }
    assert config.quota.fallback_resume_minutes == 3


@pytest.mark.asyncio
async def test_budget_is_injected_accumulated_and_enforced(tmp_path) -> None:
    budget = BudgetConfig(
        run_tokens=100,
        run_wall_clock_seconds=100,
        roles={SystemRole.S5_POLICY: {"tokens": 5, "wall_clock_seconds": 10}},
    )
    platform = await Platform.create(
        run_id="run-wave2-budget",
        runs_dir=tmp_path,
        run_config=_fake_run_config(budget=budget),
        clock=FakeClock(),
    )
    try:
        system = platform.systems[SystemRole.S5_POLICY][0]
        node_id = system.system_id
        state = platform.node_run_states[(platform.run_id, node_id)]
        authority = next(
            value for value in platform.authorities.values() if value.subject_node_id == node_id
        )
        runtime = platform.runtimes[SystemRole.S5_POLICY]
        assert isinstance(runtime, FakeAgentRuntime)
        runtime.tokens_in = 2
        runtime.tokens_out = 3
        runtime.tokens_cache_read = 1
        runtime.latency = 0.004

        result = await system.sub_agents[0].respond("consume")
        assert result.tokens_cache_read == 1
        assert state.budget == {"tokens": 5.0, "wall_clock_seconds": 10.0}
        assert authority.budget_envelope == state.budget
        assert state.cost_consumed == {
            "tokens_in": 2.0,
            "tokens_out": 3.0,
            "tokens_cache_read": 1.0,
            "tokens_total": 6.0,
            "wall_clock_ms": 4.0,
            "node_running_ms": 0.0,
        }

        with pytest.raises(BudgetExceededError):
            await system.sub_agents[0].respond("must be rejected")
        assert len(runtime.invocations) == 1
        assert platform._escalations.requests

        await asyncio.sleep(0.05)
        event_types = [event["event_type"] for event in read_all(platform.run_dir / "events.jsonl")]
        assert "budget_consumed" in event_types
        assert "budget_exceeded" in event_types
        assert "escalation_requested" in event_types
    finally:
        await platform.shutdown()


@pytest.mark.asyncio
async def test_agent_result_quota_exhausted_suspends_node(tmp_path) -> None:
    reset_at = datetime(2025, 1, 1, 0, 5, tzinfo=timezone.utc)

    def quota_result(_request) -> AgentResult:
        return AgentResult(
            text="",
            tokens_in=1,
            tokens_out=0,
            tokens_cache_read=0,
            latency_ms=2,
            model="fake/quota",
            backend="fake",
            session_ref=None,
            quota_exhausted=True,
            quota_reset_at=reset_at,
        )

    platform = await Platform.create(
        run_id="run-wave2-quota-result",
        runs_dir=tmp_path,
        run_config=_fake_run_config(),
        clock=FakeClock(),
    )
    try:
        await platform.start()
        system = platform.systems[SystemRole.S5_POLICY][0]
        runtime = platform.runtimes[SystemRole.S5_POLICY]
        assert isinstance(runtime, FakeAgentRuntime)
        runtime.response = quota_result

        with pytest.raises(QuotaExhaustedError):
            await system.sub_agents[0].respond("quota")

        state = platform.node_run_states[(platform.run_id, system.system_id)]
        assert platform.nodes[system.system_id].status is NodeStatus.SUSPENDED
        assert state.status is NodeStatus.SUSPENDED
        assert state.cost_consumed["tokens_total"] == 1
        assert platform.quota_monitor.timer_count == 1
    finally:
        await platform.shutdown()
    assert platform.quota_monitor.timer_count == 0


@pytest.mark.asyncio
async def test_quota_resume_requeues_inflight_and_suspended_messages(tmp_path) -> None:
    clock = FakeClock()
    path = tmp_path / "events.jsonl"
    writer = EventLogWriter(run_id="run-quota-monitor", path=path, clock=clock)
    await writer.start()
    bus = MessageBus(writer)
    node = Node(
        id="s1",
        parent_id="s3",
        vsm_position=SystemRole.S1_WORKER,
        status=NodeStatus.RUNNING,
    )
    state = NodeRunState(run_id="run-quota-monitor", node_id="s1", status=NodeStatus.RUNNING)
    wake = asyncio.Event()
    observed_delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        observed_delays.append(delay)
        await wake.wait()

    monitor = QuotaMonitor(
        eventlog=writer,
        bus=bus,
        clock=clock,
        nodes={"s1": node},
        node_run_states={("run-quota-monitor", "s1"): state},
        run_id="run-quota-monitor",
        fallback_resume_minutes=1,
        sleep=fake_sleep,
    )
    queue = bus.subscribe("s1", ChannelId.S1_S3)

    def message(message_id: str) -> Message:
        return Message(
            message_id=message_id,
            sender_role=SystemRole.S3_ALLOCATOR,
            sender_id="s3",
            receiver_role=SystemRole.S1_WORKER,
            receiver_id="s1",
            channel=ChannelId.S1_S3,
            payload={"id": message_id},
            timestamp_ms=0,
        )

    first = message("inflight")
    second = message("arrived-while-suspended")
    try:
        assert (await bus.send(first)).delivered
        assert await queue.get() is first
        reset_at = await monitor.suspend("s1", None, first)
        assert node.status is NodeStatus.SUSPENDED
        assert (await bus.send(second)).delivered
        assert queue.empty()

        await asyncio.sleep(0)
        assert observed_delays == [60.0]
        clock.advance(60)
        wake.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert reset_at == clock.now()
        assert node.status is NodeStatus.RUNNING
        assert state.status is NodeStatus.RUNNING
        assert [await queue.get(), await queue.get()] == [first, second]
    finally:
        await monitor.shutdown()
        await writer.stop()
    assert monitor.timer_count == 0
