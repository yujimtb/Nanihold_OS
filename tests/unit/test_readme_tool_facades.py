from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from vsm.agents import HumanAgent
from vsm.authority import ParentAuthority
from vsm.llm.fake import FakeLLMProvider
from vsm.memory import SearchScope, TaskSummary
from vsm.nodes import Node, NodeSource, NodeStatus
from vsm.roles import SystemRole
from vsm.tools import (
    HumanReviewFacade,
    HumanReviewRequest,
    IndexedTaskSummary,
    LLMCallFacade,
    LLMCallRequest,
    NodeControlFacade,
    NodeControlRequest,
    SearchPastSubtasksFacade,
    SearchPastSubtasksRequest,
    SpawnChildFacade,
    SpawnChildRequest,
    SpawnChildResult,
    TaskSummaryIndex,
    ToolEffect,
    ToolInvocation,
)


def _authority(
    effects: frozenset[ToolEffect],
    *,
    max_spawn_count: int = 0,
    termination_authority: bool = False,
) -> ParentAuthority:
    return ParentAuthority(
        authority_id="auth-tools",
        issuer_node_id="parent",
        subject_node_id="child",
        issued_at=datetime.now(timezone.utc),
        allowed_tool_classes=effects,
        max_spawn_count=max_spawn_count,
        termination_authority=termination_authority,
    )


@pytest.mark.asyncio
async def test_llm_call_facade_returns_invocation_and_result_payload() -> None:
    provider = FakeLLMProvider(response="answer", model="fake/model", tokens_in=3, tokens_out=5)
    invocation, result = await LLMCallFacade().call(
        LLMCallRequest(prompt="hello", requested_by="node-1"),
        _authority(frozenset({ToolEffect.EXTERNAL_READ})),
        provider,
    )

    assert invocation.tool_name == "llm_call"
    assert invocation.effect is ToolEffect.EXTERNAL_READ
    assert invocation.idempotency_key is None
    assert result.to_payload() == {
        "model": "fake/model",
        "response": "answer",
        "latency_ms": 0,
        "tokens_in": 3,
        "tokens_out": 5,
    }


@pytest.mark.asyncio
async def test_spawn_child_facade_runs_spawner_and_is_idempotent() -> None:
    calls: list[tuple[SpawnChildRequest, ToolInvocation]] = []

    async def runner(
        request: SpawnChildRequest,
        invocation: ToolInvocation,
    ) -> SpawnChildResult:
        calls.append((request, invocation))
        return SpawnChildResult(node_id="s1-node")

    facade = SpawnChildFacade(runner=runner)
    request = SpawnChildRequest(
        spawn_key="spawn-1",
        requested_by="s3",
        specialization="backend",
        initial_assignment={"work": "implement"},
    )
    authority = _authority(frozenset({ToolEffect.CONTROL}), max_spawn_count=1)

    first_invocation, first_result = await facade.spawn_child(request, authority)
    second_invocation, second_result = await facade.spawn_child(request, authority)

    assert first_invocation.tool_name == "spawn_child"
    assert first_invocation.idempotency_key == "spawn-1"
    assert second_invocation.idempotency_key == "spawn-1"
    assert first_result.to_payload() == {"node_id": "s1-node"}
    assert second_result == first_result
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_spawn_child_facade_rejects_authority_limits() -> None:
    async def runner(
        request: SpawnChildRequest,
        invocation: ToolInvocation,
    ) -> SpawnChildResult:
        return SpawnChildResult(node_id="s1-node")

    facade = SpawnChildFacade(runner=runner)
    authority = _authority(frozenset({ToolEffect.CONTROL}), max_spawn_count=1)
    await facade.spawn_child(
        SpawnChildRequest(
            spawn_key="spawn-1",
            requested_by="s3",
            specialization="backend",
            initial_assignment="work",
        ),
        authority,
    )

    with pytest.raises(PermissionError, match="max_spawn_count"):
        await facade.spawn_child(
            SpawnChildRequest(
                spawn_key="spawn-2",
                requested_by="s3",
                specialization="test",
                initial_assignment="work",
            ),
            authority,
        )


def test_search_past_subtasks_uses_persistent_jsonl_index(tmp_path: Path) -> None:
    index_path = tmp_path / "task-summary-index.jsonl"
    index = TaskSummaryIndex(index_path)
    index.add(
        IndexedTaskSummary(
            summary_id="sum-1",
            run_id="run-1",
            node_id="node-1",
            scope=SearchScope.KNOWLEDGE_INDEX,
            summary=TaskSummary(
                goal_achieved=True,
                approach="implemented persistent search",
                reusability_hints=("reuse jsonl index",),
            ),
        )
    )

    invocation, results = SearchPastSubtasksFacade().search(
        SearchPastSubtasksRequest(
            query="jsonl",
            requested_by="node-2",
            index_path=index_path,
            scope=SearchScope.KNOWLEDGE_INDEX,
            limit=5,
        )
    )

    assert index_path.read_text(encoding="utf-8")
    assert invocation.tool_name == "search_past_subtasks"
    assert invocation.effect is ToolEffect.PURE_READ
    assert invocation.payload["result"][0]["summary_id"] == "sum-1"
    assert [entry.summary_id for entry in results] == ["sum-1"]

    with pytest.raises(PermissionError, match="PURE_READ"):
        SearchPastSubtasksFacade().search(
            SearchPastSubtasksRequest(
                query="jsonl",
                requested_by="node-2",
                index_path=index_path,
                scope=SearchScope.KNOWLEDGE_INDEX,
            ),
            _authority(frozenset({ToolEffect.CONTROL})),
        )


def test_human_review_facade_records_review_request() -> None:
    human = HumanAgent(human_id="h1", display_name="Reviewer")
    facade = HumanReviewFacade()
    request = HumanReviewRequest(
        review_key="review-1",
        requested_by="auditor",
        reason="risk",
        subject="deployment",
        human=human,
    )

    first = facade.request_human_review(request)
    second = facade.request_human_review(request)

    assert first.tool_name == "request_human_review"
    assert first.effect is ToolEffect.HUMAN
    assert first.payload["result"] == second.payload["result"]
    assert first.payload["human"]["human_id"] == "h1"

    with pytest.raises(PermissionError, match="HUMAN"):
        facade.request_human_review(
            HumanReviewRequest(
                review_key="review-2",
                requested_by="auditor",
                reason="risk",
                subject="deployment",
            ),
            _authority(frozenset({ToolEffect.CONTROL})),
        )


def test_node_control_facade_enforces_transitions_and_termination_authority() -> None:
    facade = NodeControlFacade()
    node = Node(
        id="node-1",
        parent_id="parent",
        vsm_position=SystemRole.S1_WORKER,
        status=NodeStatus.RUNNING,
    )
    control_authority = _authority(
        frozenset({ToolEffect.CONTROL}),
        termination_authority=True,
    )

    suspended = facade.suspend_node(
        NodeControlRequest(control_key="suspend-1", requested_by="s3", node=node),
        control_authority,
    )
    resumed = facade.resume_node(
        NodeControlRequest(control_key="resume-1", requested_by="s3", node=node),
        control_authority,
    )
    terminated = facade.terminate_node(
        NodeControlRequest(control_key="term-1", requested_by="s3", node=node),
        control_authority,
    )

    assert suspended.tool_name == "suspend_node"
    assert resumed.tool_name == "resume_node"
    assert terminated.tool_name == "terminate_node"
    assert node.status is NodeStatus.TERMINATED


def test_node_control_facade_rejects_invalid_control() -> None:
    facade = NodeControlFacade()
    node = Node(
        id="node-1",
        parent_id="parent",
        vsm_position=SystemRole.S1_WORKER,
        status=NodeStatus.COMPLETED,
    )
    authority = _authority(frozenset({ToolEffect.CONTROL}))

    with pytest.raises(ValueError, match="invalid Node lifecycle transition"):
        facade.resume_node(
            NodeControlRequest(control_key="resume-1", requested_by="s3", node=node),
            authority,
        )

    running_static = Node(
        id="static",
        parent_id=None,
        vsm_position=SystemRole.S5_POLICY,
        status=NodeStatus.RUNNING,
        terminable=False,
        source=NodeSource.CONFIG,
    )
    with pytest.raises(PermissionError, match="termination_authority"):
        facade.terminate_node(
            NodeControlRequest(control_key="term-1", requested_by="s3", node=running_static),
            authority,
        )
