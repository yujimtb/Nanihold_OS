from __future__ import annotations

import json

import pytest

from vsm.agents import HumanAgent
from vsm.agents.backends import FakeAgentRuntime
from vsm.clock import FakeClock
from vsm.config import (
    AlgedonicConfig,
    ConsortiumConfig,
    CoordinationConfig,
    RunConfig,
    load_config,
)
from vsm.eventlog.writer import EventLogWriter
from vsm.messaging.bus import MessageBus
from vsm.messaging.channels import ChannelId, ExternalRole
from vsm.messaging.message import Message
from vsm.nodes import Node
from vsm.roles import SystemRole
from vsm.runtime.consortium import (
    Consortium,
    ConsortiumAborted,
    NodeParticipant,
)
from vsm.systems.s2_coordinator import S2Coordinator
from vsm.systems.s5_policy import S5Policy


class _PlatformStub:
    systems: dict = {}


class _S5PlatformStub:
    systems: dict = {}

    def __init__(self):
        self.suspended: list[dict] = []

    async def suspend_node_from_algedonic(self, **kwargs):
        self.suspended.append(kwargs)


@pytest.fixture
async def eventlog(tmp_path):
    writer = EventLogWriter(
        run_id="run-wave4", path=tmp_path / "events.jsonl", clock=FakeClock()
    )
    await writer.start()
    try:
        yield writer
    finally:
        await writer.stop()


async def test_s2_ai_coordination_decides_with_reason(eventlog):
    runtime = FakeAgentRuntime(
        response=json.dumps({"decision": "s1-a を優先", "reason": "依存順序"}, ensure_ascii=False)
    )
    coordinator = S2Coordinator(
        system_id="s2",
        eventlog=eventlog,
        bus=MessageBus(eventlog),
        runtime=runtime,
        clock=FakeClock(),
        platform=_PlatformStub(),
        run_config=RunConfig(coordination=CoordinationConfig(ai_deliberation=True)),
    )

    result = await coordinator.handle_coordination_request(
        {
            "coordination_key": "coord-1",
            "scope": "run",
            "participants": ["s1-a", "s1-b"],
            "issue": "同じファイルを編集している",
            "claims": {"s1-a": "先に開始", "s1-b": "依存を解消"},
        },
        requested_by="s1-a",
    )

    assert result == {"decision": "s1-a を優先", "reason": "依存順序"}
    assert "当事者の主張" in runtime.invocations[0].prompt


async def test_s2_ai_coordination_can_be_disabled(eventlog):
    runtime = FakeAgentRuntime(response="should not be called")
    coordinator = S2Coordinator(
        system_id="s2",
        eventlog=eventlog,
        bus=MessageBus(eventlog),
        runtime=runtime,
        clock=FakeClock(),
        platform=_PlatformStub(),
        run_config=RunConfig(coordination=CoordinationConfig(ai_deliberation=False)),
    )
    result = await coordinator.handle_coordination_request(
        {
            "coordination_key": "coord-2",
            "scope": "run",
            "participants": ["s1-a", "s1-b"],
            "issue": "競合",
        },
        requested_by="s1-a",
    )
    assert result is None
    assert runtime.invocations == []


async def test_algedonic_route_bypasses_hierarchy_and_accepts_human(eventlog):
    bus = MessageBus(eventlog)
    queue = bus.subscribe("s5", ChannelId.ALGEDONIC)
    direct = Message(
        message_id="signal-node",
        sender_role=SystemRole.S1_WORKER,
        sender_id="s1",
        receiver_role=SystemRole.S5_POLICY,
        receiver_id="s5",
        channel=ChannelId.ALGEDONIC,
        payload={"severity": "pain", "reason": "blocked", "source_node_id": "s1"},
        timestamp_ms=0,
    )
    human = Message(
        message_id="signal-human",
        sender_role=ExternalRole.HUMAN,
        sender_id="human",
        receiver_role=SystemRole.S5_POLICY,
        receiver_id="s5",
        channel=ChannelId.ALGEDONIC,
        payload={"severity": "pleasure", "reason": "success", "source_node_id": "human"},
        timestamp_ms=0,
    )

    assert (await bus.send(direct)).delivered
    assert (await bus.send(human)).delivered
    assert (await queue.get()).message_id == "signal-node"
    assert (await queue.get()).message_id == "signal-human"


async def test_s5_algedonic_handler_selects_and_records_action(eventlog):
    platform = _S5PlatformStub()
    runtime = FakeAgentRuntime(
        response=json.dumps({"action": "suspend", "reason": "被害を限定"}, ensure_ascii=False)
    )
    s5 = S5Policy(
        system_id="s5",
        eventlog=eventlog,
        bus=MessageBus(eventlog),
        runtime=runtime,
        clock=FakeClock(),
        platform=platform,
        run_config=RunConfig(),
    )
    await s5._handle_algedonic(
        Message(
            message_id="signal",
            sender_role=SystemRole.S1_WORKER,
            sender_id="s1",
            receiver_role=SystemRole.S5_POLICY,
            receiver_id="s5",
            channel=ChannelId.ALGEDONIC,
            payload={"severity": "pain", "reason": "破損", "source_node_id": "s1"},
            timestamp_ms=0,
        )
    )
    assert platform.suspended == [
        {"source_node_id": "s1", "reason": "被害を限定", "requested_by": "s5"}
    ]


def _participant(node_id: str, role: SystemRole, runtime) -> NodeParticipant:
    return NodeParticipant(
        node=Node(id=node_id, parent_id=None, vsm_position=role), runtime=runtime
    )


def _consortium_runtime():
    def response(request):
        if "招集者" in request.prompt:
            return json.dumps(
                {
                    "decision": "案Aを実施",
                    "reason": "支持が多い",
                    "dissent_summary": "案Bを求める意見が1件",
                },
                ensure_ascii=False,
            )
        return "案Aを支持する。リスクは監視する。"

    return FakeAgentRuntime(response=response)


async def test_consortium_rounds_context_hook_and_human_proceed(eventlog):
    contexts: list[tuple[str, str]] = []

    def context_hook(node_id, run_id, subject, recent):
        contexts.append((node_id, recent))
        return f"hook:{run_id}:{node_id}:{subject}:{recent}"

    async def no_human_statement(_consortium_id, _timeout):
        return None

    runtime = _consortium_runtime()
    consortium = Consortium(
        run_id="run-wave4",
        eventlog=eventlog,
        config=ConsortiumConfig(
            default_rounds=2,
            human_participation="invited",
            human_timeout_seconds=1,
            human_timeout_policy="proceed",
        ),
        context_view_hook=context_hook,
        human_statement_waiter=no_human_statement,
    )
    decision = await consortium.convene(
        subject="投資判断",
        participants=(
            _participant("s5", SystemRole.S5_POLICY, runtime),
            _participant("s4", SystemRole.S4_SCANNER, runtime),
        ),
        convener_node_id="s5",
        human=HumanAgent(human_id="owner", display_name="Owner"),
    )

    assert decision.decision == "案Aを実施"
    assert len(decision.statements) == 4
    assert {item.round_number for item in decision.statements} == {1, 2}
    assert len(contexts) == 5
    assert all(invocation.context_view.startswith("hook:") for invocation in runtime.invocations)


async def test_consortium_human_timeout_abort(eventlog):
    async def no_human_statement(_consortium_id, _timeout):
        return None

    runtime = _consortium_runtime()
    consortium = Consortium(
        run_id="run-wave4",
        eventlog=eventlog,
        config=ConsortiumConfig(
            default_rounds=1,
            human_participation="invited",
            human_timeout_seconds=1,
            human_timeout_policy="abort",
        ),
        human_statement_waiter=no_human_statement,
    )
    with pytest.raises(ConsortiumAborted):
        await consortium.convene(
            subject="停止判断",
            participants=(_participant("s5", SystemRole.S5_POLICY, runtime),),
            convener_node_id="s5",
        )


def test_wave4_config_sections_are_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("LITELLM_PROVIDER", raising=False)
    path = tmp_path / "vsm.toml"
    path.write_text(
        """
[coordination]
ai_deliberation = false
[algedonic]
notify_human = false
[consortium]
default_rounds = 3
human_participation = "none"
human_timeout_seconds = 12
human_timeout_policy = "abort"
""",
        encoding="utf-8",
    )
    _, config = load_config(path)
    assert config.coordination == CoordinationConfig(ai_deliberation=False)
    assert config.algedonic == AlgedonicConfig(notify_human=False)
    assert config.consortium == ConsortiumConfig(
        default_rounds=3,
        human_participation="none",
        human_timeout_seconds=12,
        human_timeout_policy="abort",
    )
