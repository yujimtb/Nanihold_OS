from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from vsm.agents.backends import FakeAgentRuntime
from vsm.agents.backends._common import (
    detect_quota_kind,
    parse_quota_reset_at,
    terminate_process_group,
)
from vsm.clock import FakeClock
from vsm.config import AgentsConfig, RunConfig
from vsm.eventlog.reader import read_all
from vsm.eventlog.writer import EventLogWriter
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId
from vsm.messaging.message import Message
from vsm.nodes import Node, NodeRunState, NodeStatus
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform
from vsm.runtime.quota import QuotaMonitor


def _message(message_id: str, receiver_id: str) -> Message:
    return Message(
        message_id=message_id,
        sender_role=SystemRole.S3_ALLOCATOR,
        sender_id="s3",
        receiver_role=SystemRole.S1_WORKER,
        receiver_id=receiver_id,
        channel=ChannelId.S1_S3,
        payload={"message_id": message_id},
        timestamp_ms=0,
    )


def test_quota_kind_is_detected_from_cli_diagnostics() -> None:
    assert detect_quota_kind("weekly usage limit reached") == "weekly"
    assert detect_quota_kind("5-hour quota limit reached") == "five_hour"
    assert detect_quota_kind('{"window_minutes": 300}') == "five_hour"
    assert detect_quota_kind('{"window_minutes": 10080}') == "weekly"
    assert detect_quota_kind("5-hour and weekly limits reached") == "unknown"
    assert detect_quota_kind("quota limit reached") == "unknown"


def test_quota_reset_is_parsed_exactly_from_structured_diagnostics() -> None:
    assert parse_quota_reset_at(
        '{"reset_at":"2026-07-19T12:04:05.678+09:00"}'
    ) == datetime(2026, 7, 19, 3, 4, 5, 678000, tzinfo=timezone.utc)
    assert parse_quota_reset_at('{"resets_at":1784420645}') == datetime.fromtimestamp(
        1784420645, tz=timezone.utc
    )


@pytest.mark.asyncio
async def test_quota_pool_probe_then_resumes_nodes_sequentially(tmp_path) -> None:
    clock = FakeClock()
    writer = EventLogWriter(
        run_id="resume-pool-test",
        path=tmp_path / "events.jsonl",
        clock=clock,
    )
    await writer.start()
    bus = MessageBus(writer)
    nodes = {
        node_id: Node(
            id=node_id,
            parent_id=None,
            vsm_position=SystemRole.S1_WORKER,
            status=NodeStatus.RUNNING,
        )
        for node_id in ("n1", "n2", "n3")
    }
    states = {
        ("resume-pool-test", node_id): NodeRunState(
            run_id="resume-pool-test", node_id=node_id, status=NodeStatus.RUNNING
        )
        for node_id in nodes
    }
    queues = {
        node_id: bus.subscribe(node_id, ChannelId.S1_S3) for node_id in nodes
    }
    probe_calls: list[str] = []
    resumed: list[str] = []

    async def probe(pool: str) -> bool:
        probe_calls.append(pool)
        return True

    monitor = QuotaMonitor(
        eventlog=writer,
        bus=bus,
        clock=clock,
        nodes=nodes,
        node_run_states=states,
        run_id="resume-pool-test",
        node_pools={node_id: "claude-subscription" for node_id in nodes},
        probe=probe,
        on_node_resumed=resumed.append,
    )
    try:
        await monitor.suspend(
            "n2",
            clock.now() + timedelta(seconds=1),
            _message("inflight", "n2"),
            quota_kind="five_hour",
        )
        assert all(node.status is NodeStatus.QUOTA_WAIT for node in nodes.values())
        await bus.send(_message("during-wait", "n1"))
        await monitor.resume("n2")
        assert probe_calls == ["claude-subscription"]
        assert resumed == ["n1", "n2", "n3"]
        assert all(node.status is NodeStatus.RUNNING for node in nodes.values())
        assert await queues["n1"].get() == _message("during-wait", "n1")
        assert await queues["n2"].get() == _message("inflight", "n2")
        events = read_all(tmp_path / "events.jsonl")
        assert [event["event_type"] for event in events].count("quota_pool_opened") == 1
        assert [event["event_type"] for event in events].count("quota_pool_closed") == 1
    finally:
        await monitor.shutdown()
        await writer.stop()


def _platform_config() -> RunConfig:
    roles = {role: "fake" for role in SystemRole}
    roles[SystemRole.S3_ALLOCATOR] = ""
    return RunConfig(agents=AgentsConfig(default_backend="fake", roles=roles))


def _platform_runtimes() -> dict[SystemRole, FakeAgentRuntime | None]:
    return {
        role: (
            None
            if role is SystemRole.S3_ALLOCATOR
            else FakeAgentRuntime(quota_pool="claude-subscription")
        )
        for role in SystemRole
    }


@pytest.mark.asyncio
async def test_platform_restarts_from_quota_state_and_excludes_wait_time(tmp_path) -> None:
    clock = FakeClock()
    config = _platform_config()
    first = await Platform.create(
        run_id="platform-resume-test",
        runs_dir=tmp_path,
        run_config=config,
        runtime_overrides=_platform_runtimes(),
        clock=clock,
    )
    await first.start()
    s5_id = first.systems[SystemRole.S5_POLICY][0].system_id
    clock.advance(10)
    await first.quota_monitor.suspend(
        s5_id,
        clock.now() + timedelta(seconds=1),
        quota_kind="weekly",
    )
    clock.advance(100)
    await first.shutdown()

    state_path = tmp_path / "platform-resume-test" / "quota-state.json"
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["pools"][0]["quota_kind"] == "weekly"

    second = await Platform.create(
        run_id="platform-resume-test",
        runs_dir=tmp_path,
        run_config=config,
        runtime_overrides=_platform_runtimes(),
        clock=clock,
        resume=True,
    )
    try:
        assert second.systems[SystemRole.S5_POLICY][0].system_id == s5_id
        assert second.nodes[s5_id].status is NodeStatus.QUOTA_WAIT
        await second.start()
        await second.quota_monitor.resume(s5_id)
        clock.advance(5)
        await second.before_agent_invoke(s5_id)
        state = second.node_run_states[(second.run_id, s5_id)]
        assert state.cost_consumed["node_running_ms"] == 5000
        assert all(node.status is NodeStatus.RUNNING for node in second.nodes.values())
    finally:
        await second.shutdown()


@pytest.mark.asyncio
async def test_process_group_kill_is_deterministic_with_mock_process(monkeypatch) -> None:
    import vsm.agents.backends._common as common

    calls: list[tuple[int, int]] = []

    class Process:
        pid = 1234
        returncode = None

        async def communicate(self):
            self.returncode = -15
            return b"", b""

    process = Process()
    monkeypatch.setattr(common.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        common.os,
        "killpg",
        lambda pid, signal: calls.append((pid, signal)),
    )
    await terminate_process_group(process)
    assert calls and calls[0][0] == 1234
    assert process.returncode == -15
