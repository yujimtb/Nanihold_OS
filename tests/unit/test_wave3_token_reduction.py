from __future__ import annotations

import json
from pathlib import Path

import pytest

from vsm.agents.backends.fake import FakeAgentRuntime
from vsm.agents.runtime import AgentRequest, AgentResult, AgentRuntimeError
from vsm.clock import SystemClock
from vsm.config import AgentsConfig, RunConfig
from vsm.eventlog.writer import EventLogWriter
from vsm.memory import SearchScope, TaskSummary
from vsm.memory.builder import ContextViewBuilder
from vsm.messaging.channels import ChannelId
from vsm.messaging.message import Message
from vsm.nodes import Node, NodeRunState
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform
from vsm.systems.base import System
from vsm.tools.search import IndexedTaskSummary, TaskSummaryIndex


class _DummySystem(System):
    async def run(self) -> None:
        await __import__("asyncio").Event().wait()


def _result(text: str, session_ref: str) -> AgentResult:
    return AgentResult(
        text=text,
        tokens_in=1,
        tokens_out=1,
        tokens_cache_read=0,
        latency_ms=1,
        model="fake/model",
        backend="fake",
        session_ref=session_ref,
    )


def test_context_view_builder_is_deterministic(tmp_path: Path) -> None:
    run_id = "run-context"
    parent = Node(id="parent", parent_id=None, vsm_position=SystemRole.S3_ALLOCATOR)
    node = Node(
        id="node",
        parent_id="parent",
        vsm_position=SystemRole.S1_WORKER,
        goal="テストを完了する",
        child_ids=["child"],
        artifact_refs=["artifacts/result.txt"],
    )
    child = Node(id="child", parent_id="node", vsm_position=SystemRole.S1_WORKER)
    events_path = tmp_path / "events.jsonl"
    events = [
        {
            "run_id": run_id,
            "seq": 1,
            "node_id": "parent",
            "actor_id": "parent",
            "event_type": "policy_decision",
            "payload": {"directive": "境界条件を先に検証する"},
        },
        {
            "run_id": run_id,
            "seq": 2,
            "node_id": "node",
            "actor_id": "node",
            "event_type": "llm_invocation",
            "payload": {"response": "実装を開始した"},
        },
    ]
    events_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    artifact = tmp_path / "artifacts" / "result.txt"
    artifact.parent.mkdir()
    artifact.write_text("検証済みの成果物", encoding="utf-8")
    index = TaskSummaryIndex(tmp_path / "memory" / "task-summaries.jsonl")
    index.add(
        IndexedTaskSummary(
            summary_id="summary-1",
            run_id=run_id,
            node_id="child",
            summary=TaskSummary(goal_achieved=True, approach="単体テストを追加"),
            scope=SearchScope.DIRECT_CHILD_SUMMARIES,
        )
    )
    builder = ContextViewBuilder(
        nodes={item.id: item for item in (parent, node, child)},
        events_path=events_path,
        summary_index=index,
        run_dir=tmp_path,
    )

    first = builder.build("node", run_id)
    second = builder.build("node", run_id)

    assert first == second
    assert "境界条件を先に検証する" in first
    assert "単体テストを追加" in first
    assert "検証済みの成果物" in first
    assert "実装を開始した" in first


@pytest.mark.asyncio
async def test_same_node_resumes_and_omits_context_view(tmp_path: Path) -> None:
    runtime = FakeAgentRuntime(response="ok", session_ref="session-1")
    system, writer, state = await _bound_system(tmp_path, runtime)
    try:
        await system.sub_agents[0].respond("役割: test\n今回の指示: first")
        await system.sub_agents[0].respond("役割: test\n今回の指示: second")
    finally:
        await writer.stop()

    assert runtime.invocations[0].session_ref is None
    assert runtime.invocations[0].context_view is not None
    assert runtime.invocations[1].session_ref == "session-1"
    assert runtime.invocations[1].context_view is None
    assert state.session_refs == {"fake": "session-1"}


@pytest.mark.asyncio
async def test_resume_failure_retries_new_session_with_full_context(tmp_path: Path) -> None:
    class ResumeFailingRuntime:
        backend_name = "fake"
        timeout_seconds = 1.0

        def __init__(self) -> None:
            self.invocations: list[AgentRequest] = []

        async def invoke(self, request: AgentRequest) -> AgentResult:
            self.invocations.append(request)
            if request.session_ref is not None:
                raise AgentRuntimeError(backend="fake", code="session_missing", message="gone")
            return _result("recovered", "session-new")

    runtime = ResumeFailingRuntime()
    system, writer, state = await _bound_system(tmp_path, runtime)
    state.session_refs["fake"] = "session-old"
    try:
        response = await system.sub_agents[0].respond("役割: test\n今回の指示: retry")
    finally:
        await writer.stop()

    assert response.text == "recovered"
    assert runtime.invocations[0].session_ref == "session-old"
    assert runtime.invocations[0].context_view is None
    assert runtime.invocations[1].session_ref is None
    assert runtime.invocations[1].context_view is not None
    assert state.session_refs == {"fake": "session-new"}


@pytest.mark.asyncio
async def test_s1_completion_registers_task_summary(tmp_path: Path) -> None:
    roles = {role: "fake" for role in SystemRole}
    roles[SystemRole.S3_ALLOCATOR] = ""
    config = RunConfig(agents=AgentsConfig(default_backend="fake", roles=roles))
    s1_runtime = FakeAgentRuntime(response="実装してテストした", session_ref="s1-session")
    runtime_overrides = {
        role: (None if role is SystemRole.S3_ALLOCATOR else FakeAgentRuntime())
        for role in SystemRole
    }
    runtime_overrides[SystemRole.S1_WORKER] = s1_runtime
    platform = await Platform.create(
        run_id="run-summary",
        runs_dir=tmp_path,
        run_config=config,
        runtime_overrides=runtime_overrides,
    )
    try:
        s1 = await platform.spawn_s1(specialization="test", initial_assignment="対象を検証")
        s3 = platform.systems[SystemRole.S3_ALLOCATOR][0]
        await s1._execute_assignment(
            Message(
                message_id="message-1",
                sender_role=SystemRole.S3_ALLOCATOR,
                sender_id=s3.system_id,
                receiver_role=SystemRole.S1_WORKER,
                receiver_id=s1.system_id,
                channel=ChannelId.S1_S3,
                payload={"work_item_id": "work-1", "assignment": {"task": "検証"}},
                timestamp_ms=0,
            )
        )
        entries = platform.task_summary_index.list_for_nodes(
            run_id=platform.run_id,
            node_ids={s1.system_id},
        )
        assert len(entries) == 1
        assert entries[0].summary.goal_achieved is True
        assert entries[0].summary.approach == "実装してテストした"
        assert platform.nodes[s1.system_id].summary_refs == [entries[0].summary_id]
    finally:
        await platform.shutdown()


async def _bound_system(tmp_path: Path, runtime):
    events_path = tmp_path / "events.jsonl"
    events_path.touch()
    writer = EventLogWriter(run_id="run-session", path=events_path, clock=SystemClock())
    await writer.start()
    system = _DummySystem(
        system_id="node-1",
        role=SystemRole.S1_WORKER,
        eventlog=writer,
        runtime=runtime,
        clock=SystemClock(),
    )
    system.register_sub_agent("default")
    state = NodeRunState(run_id="run-session", node_id="node-1")
    builder = ContextViewBuilder(
        nodes={"node-1": Node(id="node-1", parent_id=None, vsm_position=SystemRole.S1_WORKER)},
        events_path=events_path,
        summary_index=TaskSummaryIndex(tmp_path / "summaries.jsonl"),
        run_dir=tmp_path,
    )
    system.bind_node_context(
        run_id="run-session",
        run_state=state,
        context_builder=builder,
        resume_within_run=True,
    )
    return system, writer, state
