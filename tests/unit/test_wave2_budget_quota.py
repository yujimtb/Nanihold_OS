from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from vsm.agents.backends.fake import FakeAgentRuntime
from vsm.agents.runtime import AgentResult
from vsm.clock import FakeClock
from vsm.config import AgentsConfig, BudgetConfig, RunConfig, load_config
from vsm.errors import (
    BudgetExceededError,
    QuotaExhaustedError,
    QuotaResolutionRequiredError,
)
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
invocation_initial_tokens = 20
invocation_initial_wall_clock_seconds = 2
invocation_safety_multiplier = 1.5
[budget.roles]
S5_POLICY = { tokens = 100, wall_clock_seconds = 7 }
[quota]
suspend_on_exhausted = true
""",
        encoding="utf-8",
    )

    _, config = load_config(path)

    assert config.budget.run_tokens == 1234
    assert config.budget.envelope_for(SystemRole.S5_POLICY) == {
        "tokens": 100.0,
        "wall_clock_seconds": 7.0,
    }
    assert config.budget.invocation_initial_tokens == 20
    assert config.budget.invocation_initial_wall_clock_seconds == 2
    assert config.budget.invocation_safety_multiplier == 1.5
    assert config.quota.suspend_on_exhausted is True


@pytest.mark.asyncio
async def test_budget_is_injected_accumulated_and_enforced(tmp_path) -> None:
    budget = BudgetConfig(
        run_tokens=100,
        run_wall_clock_seconds=100,
        invocation_initial_tokens=4,
        invocation_initial_wall_clock_seconds=0.001,
        invocation_safety_multiplier=1.5,
        roles={SystemRole.S5_POLICY: {"tokens": 10, "wall_clock_seconds": 10}},
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
        assert state.budget == {"tokens": 10.0, "wall_clock_seconds": 10.0}
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
async def test_budget_preflight_rejects_before_runtime_when_remaining_is_insufficient(
    tmp_path,
) -> None:
    budget = BudgetConfig(
        run_tokens=100,
        run_wall_clock_seconds=100,
        invocation_initial_tokens=4,
        invocation_initial_wall_clock_seconds=0.001,
        invocation_safety_multiplier=1.5,
        roles={SystemRole.S5_POLICY: {"tokens": 5, "wall_clock_seconds": 10}},
    )
    platform = await Platform.create(
        run_id="run-wave2-budget-preflight",
        runs_dir=tmp_path,
        run_config=_fake_run_config(budget=budget),
        clock=FakeClock(),
    )
    try:
        system = platform.systems[SystemRole.S5_POLICY][0]
        runtime = platform.runtimes[SystemRole.S5_POLICY]
        assert isinstance(runtime, FakeAgentRuntime)

        with pytest.raises(BudgetExceededError):
            await system.sub_agents[0].respond("must not start")

        assert runtime.invocations == []
        assert platform._escalations.requests
        await asyncio.sleep(0.05)
        exceeded = next(
            event
            for event in read_all(platform.run_dir / "events.jsonl")
            if event["event_type"] == "budget_exceeded"
        )
        assert exceeded["payload"]["invocation_estimate"]["tokens"] == 6
        assert exceeded["payload"]["remaining_before_invocation"]["node_tokens"] == 5
    finally:
        await platform.shutdown()


@pytest.mark.asyncio
async def test_budget_preflight_rejects_wall_clock_shortage_before_runtime(
    tmp_path,
) -> None:
    budget = BudgetConfig(
        run_tokens=100,
        run_wall_clock_seconds=100,
        invocation_initial_tokens=4,
        invocation_initial_wall_clock_seconds=1,
        invocation_safety_multiplier=1.5,
        roles={SystemRole.S5_POLICY: {"tokens": 100, "wall_clock_seconds": 1}},
    )
    platform = await Platform.create(
        run_id="run-wave2-budget-wall-preflight",
        runs_dir=tmp_path,
        run_config=_fake_run_config(budget=budget),
        clock=FakeClock(),
    )
    try:
        system = platform.systems[SystemRole.S5_POLICY][0]
        runtime = platform.runtimes[SystemRole.S5_POLICY]
        assert isinstance(runtime, FakeAgentRuntime)

        with pytest.raises(BudgetExceededError):
            await system.sub_agents[0].respond("must not start")

        assert runtime.invocations == []
        await asyncio.sleep(0.05)
        exceeded = next(
            event
            for event in read_all(platform.run_dir / "events.jsonl")
            if event["event_type"] == "budget_exceeded"
        )
        assert exceeded["payload"]["reasons"] == ["node_wall_clock"]
        assert (
            exceeded["payload"]["invocation_estimate"]["wall_clock_seconds"]
            == 1.5
        )
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
            quota_kind="five_hour",
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
        reset_at = await monitor.suspend(
            "s1",
            clock.now() + timedelta(seconds=60),
            first,
            quota_kind="five_hour",
        )
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


@pytest.mark.asyncio
async def test_unknown_quota_is_durable_fail_fast_without_auto_resume(tmp_path) -> None:
    clock = FakeClock()
    writer = EventLogWriter(
        run_id="run-quota-unknown",
        path=tmp_path / "events.jsonl",
        clock=clock,
    )
    await writer.start()
    bus = MessageBus(writer)
    node = Node(
        id="s1",
        parent_id="s3",
        vsm_position=SystemRole.S1_WORKER,
        status=NodeStatus.RUNNING,
    )
    state = NodeRunState(
        run_id="run-quota-unknown", node_id="s1", status=NodeStatus.RUNNING
    )
    observed_delays: list[float] = []

    async def fail_if_scheduled(delay: float) -> None:
        observed_delays.append(delay)

    state_path = tmp_path / "quota-state.json"
    monitor = QuotaMonitor(
        eventlog=writer,
        bus=bus,
        clock=clock,
        nodes={"s1": node},
        node_run_states={("run-quota-unknown", "s1"): state},
        run_id="run-quota-unknown",
        state_path=state_path,
        sleep=fail_if_scheduled,
    )
    try:
        with pytest.raises(QuotaResolutionRequiredError):
            await monitor.suspend("s1", None, quota_kind="unknown")
        await asyncio.sleep(0)

        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["version"] == 2
        assert persisted["pools"] == [
            {
                "node_ids": ["s1"],
                "pool": "node:s1",
                "quota_kind": "unknown",
                "reset_at": None,
                "status": "human_review_required",
            }
        ]
        assert node.status is NodeStatus.FAILED
        assert state.status is NodeStatus.FAILED
        assert monitor.requires_human_resolution("s1")
        assert monitor.timer_count == 0
        assert observed_delays == []
    finally:
        await monitor.shutdown()
        await writer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("quota_kind", ["five_hour", "weekly"])
async def test_known_quota_reset_is_persisted_and_restored_exactly(
    tmp_path, quota_kind: str
) -> None:
    run_id = f"run-quota-{quota_kind}"
    reset_at = datetime(2026, 7, 19, 3, 4, 5, 678000, tzinfo=timezone.utc)
    state_path = tmp_path / f"quota-state-{quota_kind}.json"
    clock = FakeClock()

    async def make_monitor(*, sleep):
        writer = EventLogWriter(
            run_id=run_id,
            path=tmp_path / f"events-{quota_kind}.jsonl",
            clock=clock,
        )
        await writer.start()
        bus = MessageBus(writer)
        node = Node(
            id="s1",
            parent_id="s3",
            vsm_position=SystemRole.S1_WORKER,
            status=NodeStatus.RUNNING,
        )
        state = NodeRunState(run_id=run_id, node_id="s1", status=NodeStatus.RUNNING)
        monitor = QuotaMonitor(
            eventlog=writer,
            bus=bus,
            clock=clock,
            nodes={"s1": node},
            node_run_states={(run_id, "s1"): state},
            run_id=run_id,
            state_path=state_path,
            sleep=sleep,
        )
        return writer, node, state, monitor

    first_wait = asyncio.Event()

    async def first_sleep(_delay: float) -> None:
        await first_wait.wait()

    writer, _, _, monitor = await make_monitor(sleep=first_sleep)
    try:
        assert (
            await monitor.suspend("s1", reset_at, quota_kind=quota_kind)
            == reset_at
        )
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["pools"][0]["status"] == "waiting_reset"
        assert persisted["pools"][0]["quota_kind"] == quota_kind
        assert persisted["pools"][0]["reset_at"] == "2026-07-19T03:04:05.678Z"
    finally:
        await monitor.shutdown()
        await writer.stop()

    restored_delays: list[float] = []
    restored_wait = asyncio.Event()

    async def restored_sleep(delay: float) -> None:
        restored_delays.append(delay)
        await restored_wait.wait()

    writer, node, state, restored = await make_monitor(sleep=restored_sleep)
    try:
        assert restored.pool_states["node:s1"] == {
            "status": "waiting_reset",
            "quota_kind": quota_kind,
            "reset_at": reset_at,
            "node_ids": ["s1"],
        }
        await restored.reconcile()
        await asyncio.sleep(0)
        expected_delay = max(0.0, (reset_at - clock.now()).total_seconds())
        assert restored_delays == [expected_delay]
        assert node.status is NodeStatus.QUOTA_WAIT
        assert state.status is NodeStatus.QUOTA_WAIT
    finally:
        await restored.shutdown()
        await writer.stop()
