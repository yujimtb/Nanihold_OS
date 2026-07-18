from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from vsm.agents.backends import FakeAgentRuntime
from vsm.agents.runtime import AgentRequest, AgentResult, AgentRuntimeError
from vsm.config import AgentsConfig, ConsortiumConfig, RunConfig
from vsm.eventlog.reader import read_all
from vsm.nodes import NodeStatus
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform


def _run_config() -> RunConfig:
    roles = {role: "fake" for role in SystemRole}
    roles[SystemRole.S3_ALLOCATOR] = ""
    return RunConfig(
        agents=AgentsConfig(default_backend="fake", roles=roles),
        consortium=ConsortiumConfig(
            default_rounds=1,
            human_participation="none",
        ),
    )


def _runtime_overrides() -> dict[SystemRole, FakeAgentRuntime | None]:
    return {
        role: (None if role is SystemRole.S3_ALLOCATOR else FakeAgentRuntime())
        for role in SystemRole
    }


@pytest.mark.asyncio
async def test_resume_retry_records_one_logical_invocation_and_one_budget_charge(
    tmp_path,
) -> None:
    class ResumeFailingRuntime:
        backend_name = "fake"
        timeout_seconds = 1.0

        def __init__(self) -> None:
            self.invocations: list[AgentRequest] = []

        async def invoke(self, request: AgentRequest) -> AgentResult:
            self.invocations.append(request)
            if request.session_ref is not None:
                raise AgentRuntimeError(
                    backend="fake",
                    code="session_missing",
                    message="gone",
                )
            return AgentResult(
                text="recovered",
                tokens_in=3,
                tokens_out=2,
                tokens_cache_read=1,
                latency_ms=7,
                model="fake/model",
                backend="fake",
                session_ref="session-new",
            )

    runtime = ResumeFailingRuntime()
    overrides = _runtime_overrides()
    overrides[SystemRole.S5_POLICY] = runtime  # type: ignore[assignment]
    platform = await Platform.create(
        run_id="run-merge-resume-budget",
        runs_dir=tmp_path,
        run_config=_run_config(),
        runtime_overrides=overrides,
    )
    events_path = platform.run_dir / "events.jsonl"
    system = platform.systems[SystemRole.S5_POLICY][0]
    state = platform.node_run_states[(platform.run_id, system.system_id)]
    state.session_refs["fake"] = "session-old"
    try:
        result = await system.sub_agents[0].respond("再開して実行")
        assert result.text == "recovered"
        assert len(runtime.invocations) == 2
        assert runtime.invocations[0].session_ref == "session-old"
        assert runtime.invocations[1].session_ref is None
        assert runtime.invocations[1].context_view is not None
        assert state.session_refs == {"fake": "session-new"}
        assert state.cost_consumed["tokens_total"] == 6
        assert state.cost_consumed["wall_clock_ms"] == 7
    finally:
        await platform.shutdown()

    event_types = [event["event_type"] for event in read_all(events_path)]
    assert event_types.count("tool_invoked") == 1
    assert event_types.count("budget_consumed") == 1
    assert event_types.count("llm_invocation") == 1
    assert event_types.count("llm_error") == 0


@pytest.mark.asyncio
async def test_algedonic_and_quota_suspend_do_not_double_transition(tmp_path) -> None:
    platform = await Platform.create(
        run_id="run-merge-suspend",
        runs_dir=tmp_path,
        run_config=_run_config(),
        runtime_overrides=_runtime_overrides(),
    )
    events_path = platform.run_dir / "events.jsonl"
    await platform.start()
    algedonic_node = platform.systems[SystemRole.S4_SCANNER][0].system_id
    quota_node = platform.systems[SystemRole.S2_COORDINATOR][0].system_id
    try:
        await platform.suspend_node_from_algedonic(
            source_node_id=algedonic_node,
            reason="pain",
            requested_by="s5",
        )
        with pytest.raises(ValueError, match="SUSPENDED -> SUSPENDED"):
            await platform.quota_monitor.suspend(algedonic_node, None)
        assert not platform.quota_monitor.has_pending_resume(algedonic_node)

        reset_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await platform.quota_monitor.suspend(
            quota_node, reset_at, quota_kind="five_hour"
        )
        with pytest.raises(ValueError, match="SUSPENDED -> SUSPENDED"):
            await platform.suspend_node_from_algedonic(
                source_node_id=quota_node,
                reason="duplicate",
                requested_by="s5",
            )

        assert platform.nodes[algedonic_node].status is NodeStatus.SUSPENDED
        assert platform.nodes[quota_node].status is NodeStatus.SUSPENDED
        assert platform.quota_monitor.has_pending_resume(quota_node)
        assert platform.quota_monitor.timer_count == 1
    finally:
        await platform.shutdown()

    event_types = [event["event_type"] for event in read_all(events_path)]
    assert event_types.count("node_suspended") == 1
    assert event_types.count("quota_exhausted") == 1


@pytest.mark.asyncio
async def test_platform_consortium_receives_context_view_builder_output(tmp_path) -> None:
    def response(request: AgentRequest) -> str:
        if "招集者" in request.prompt:
            return json.dumps(
                {
                    "decision": "実施",
                    "reason": "合意",
                    "dissent_summary": "なし",
                },
                ensure_ascii=False,
            )
        return "実施を支持する"

    overrides = _runtime_overrides()
    overrides[SystemRole.S4_SCANNER] = FakeAgentRuntime(response=response)
    overrides[SystemRole.S5_POLICY] = FakeAgentRuntime(response=response)
    platform = await Platform.create(
        run_id="run-merge-consortium-context",
        runs_dir=tmp_path,
        run_config=_run_config(),
        runtime_overrides=overrides,
    )
    s4 = platform.systems[SystemRole.S4_SCANNER][0]
    s5 = platform.systems[SystemRole.S5_POLICY][0]
    try:
        decision = await platform.convene_consortium(
            subject="統合判断",
            convener_node_id=s5.system_id,
            participant_node_ids=[s4.system_id, s5.system_id],
        )
        assert decision.decision == "実施"
        for role, node_id in (
            (SystemRole.S4_SCANNER, s4.system_id),
            (SystemRole.S5_POLICY, s5.system_id),
        ):
            runtime = platform.runtimes[role]
            assert isinstance(runtime, FakeAgentRuntime)
            assert runtime.invocations
            assert all(
                invocation.context_view is not None
                and "【現在の文脈】" in invocation.context_view
                and f"Node: {node_id}" in invocation.context_view
                for invocation in runtime.invocations
            )
    finally:
        await platform.shutdown()
