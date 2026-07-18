from __future__ import annotations

import asyncio

import pytest

from vsm.agents.backends import FakeAgentRuntime
from vsm.agents.runtime import AgentRequest, AgentResult
from vsm.clock import SystemClock
from vsm.config import AgentsConfig, RunConfig
from vsm.eventlog.reader import read_all
from vsm.eventlog.writer import EventLogWriter
from vsm.ids import generate_uuid
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId, ExternalRole
from vsm.messaging.message import Message
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform


def _run_config() -> RunConfig:
    roles = {role: "fake" for role in SystemRole}
    roles[SystemRole.S3_ALLOCATOR] = ""
    return RunConfig(agents=AgentsConfig(default_backend="fake", roles=roles))


def _runtime_overrides():
    return {
        role: (None if role is SystemRole.S3_ALLOCATOR else FakeAgentRuntime())
        for role in SystemRole
    }


@pytest.mark.asyncio
async def test_live_instruction_is_injected_before_next_llm_invocation(tmp_path) -> None:
    class BlockingRuntime:
        backend_name = "fake"
        timeout_seconds = 5.0

        def __init__(self) -> None:
            self.invocations: list[AgentRequest] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def invoke(self, request: AgentRequest) -> AgentResult:
            self.invocations.append(request)
            if len(self.invocations) == 1:
                self.first_started.set()
                await self.release_first.wait()
            return AgentResult(
                text="完了",
                tokens_in=1,
                tokens_out=1,
                tokens_cache_read=0,
                latency_ms=1,
                model="fake/test-model",
                backend="fake",
                session_ref=None,
            )

    runtime = BlockingRuntime()
    overrides = _runtime_overrides()
    overrides[SystemRole.S5_POLICY] = runtime
    platform = await Platform.create(
        run_id="run-instruction-boundary",
        runs_dir=tmp_path,
        run_config=_run_config(),
        runtime_overrides=overrides,
    )
    target = platform.systems[SystemRole.S5_POLICY][0]
    events_path = platform.run_dir / "events.jsonl"

    first = asyncio.create_task(target.sub_agents[0].respond("最初の作業"))
    await asyncio.wait_for(runtime.first_started.wait(), timeout=1)
    instruction_id = await platform.deliver_instruction(
        "品質を最優先し、根拠も示す", target_node=target.system_id
    )
    assert "品質を最優先" not in runtime.invocations[0].prompt

    runtime.release_first.set()
    await first
    await target.sub_agents[0].respond("次の作業")
    await platform.shutdown()

    assert len(runtime.invocations) == 2
    assert "次の作業" in runtime.invocations[1].prompt
    assert "品質を最優先し、根拠も示す" in runtime.invocations[1].prompt

    events = read_all(events_path)
    applied = [
        event
        for event in events
        if event["event_type"] == "instruction_applied"
        and event["payload"]["instruction_id"] == instruction_id
    ]
    assert len(applied) == 1
    invocation_id = applied[0]["payload"]["invocation_id"]
    invocation_started = next(
        event
        for event in events
        if event["event_type"] == "tool_invoked"
        and event["payload"]["tool_invocation_id"] == invocation_id
    )
    assert applied[0]["seq"] < invocation_started["seq"]
    assert not any(event["event_type"] == "instruction_completed" for event in events)


@pytest.mark.asyncio
async def test_message_acceptance_and_eventlog_stop_are_atomic_under_load(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "events.jsonl"
    writer = EventLogWriter("run-message-stop-race", path, SystemClock())
    await writer.start()
    bus = MessageBus(writer)
    receiver_id = "s5"
    bus.subscribe(receiver_id, ChannelId.INSTRUCTION)

    first_write_started = asyncio.Event()
    release_writer = asyncio.Event()
    original_write = writer._write_with_retry

    async def blocked_write(event):
        first_write_started.set()
        await release_writer.wait()
        await original_write(event)

    monkeypatch.setattr(writer, "_write_with_retry", blocked_write)

    def message(index: int) -> Message:
        return Message(
            message_id=generate_uuid(),
            sender_role=ExternalRole.HUMAN,
            sender_id="local-user",
            receiver_role=SystemRole.S5_POLICY,
            receiver_id=receiver_id,
            channel=ChannelId.INSTRUCTION,
            payload={
                "instruction_id": f"instruction-{index}",
                "instruction": f"追加指示 {index}",
            },
            timestamp_ms=index,
        )

    accepted = await bus.send(message(0))
    assert accepted.delivered
    await asyncio.wait_for(first_write_started.wait(), timeout=1)

    stop_task = asyncio.create_task(writer.stop())
    while writer._state != "stopping":
        await asyncio.sleep(0)
    results = await asyncio.gather(
        *(bus.send(message(index)) for index in range(1, 101)),
        return_exceptions=True,
    )
    release_writer.set()
    await stop_task

    # shutdown 受付終了後の send は成功扱いにならない。成功した Message の
    # channel_message event は sentinel より前にあり、必ず全件残る。
    assert all(isinstance(result, RuntimeError) for result in results)
    events = read_all(path)
    delivered = [event for event in events if event["event_type"] == "channel_message"]
    assert len(delivered) == 1
    assert delivered[0]["payload"]["payload"]["instruction_id"] == "instruction-0"


@pytest.mark.asyncio
async def test_platform_shutdown_reaps_all_system_receive_tasks(tmp_path) -> None:
    platform = await Platform.create(
        run_id="run-system-task-reaping",
        runs_dir=tmp_path,
        run_config=_run_config(),
        runtime_overrides=_runtime_overrides(),
    )
    await platform.start()
    await asyncio.sleep(0)
    receive_prefixes = (
        "s1_worker.",
        "s2_s1_s2_get",
        "s3_allocator.",
        "s4_task_get",
        "s4_s5_get",
        "s5_recv_",
        "s3star_",
    )
    children = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith(receive_prefixes)
    ]
    assert children

    await platform.shutdown()

    assert all(task.done() for task in children)
    assert all(
        system._task is None
        for systems in platform.systems.values()
        for system in systems
    )
