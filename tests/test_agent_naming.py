from __future__ import annotations

from pathlib import Path
import logging
import math

import pytest

from conftest import NOW, OWNER_ID, SPACE_ID, make_node
from vsm.agent_naming import AgentNameAssignment, AgentNameRegistry
from vsm.dispatcher import DependencyAwareDispatcher, PilotBinding
from vsm.errors import InvariantViolation
from vsm.kernel.models import (
    NodeKind,
    RouteSnapshot,
    RouteSnapshotState,
    WorkItem,
    WorkState,
)
from vsm.pilot.models import ModelCandidate
from vsm.pilot.production_host import (
    PilotHostReceipt,
    WorkExecutionOutcome,
    WorkExecutionResult,
)
from vsm.routing.bayesian import BayesianRouter, BenchmarkPrior


CSV = """カテゴリ,規模,意味座標,日,英,羅,いいね
居,2,秋,Aki,Autumn,Autumnus,1
糸,2,重複,Forbidden,Forbidden,Forbidden,0
天候,3,凪,Nagi,Calm,Quies,1
居,3,旗,Hata,Banner,Vexillum,1
居,1,芽,Mebae,Bud,Germen,1
"""


def candidate(provider: str, snapshot: str) -> ModelCandidate:
    return ModelCandidate(
        adapter="test-adapter",
        adapter_version="1",
        provider=provider,
        selection="exact",
        model_snapshot=snapshot,
        effort="high",
        toolset=(),
        sandbox_fingerprint="sandbox:test",
        environment_fingerprint="environment:test",
    )


def registry(tmp_path: Path) -> AgentNameRegistry:
    path = tmp_path / "Agent_name.csv"
    path.write_text(CSV, encoding="utf-8")
    return AgentNameRegistry.from_csv(path)


def allocate(registry: AgentNameRegistry, assignment_id: str, model: ModelCandidate):
    return registry.allocate(
        assignment_id=f"assignment:{assignment_id}",
        data_space_id="space:personal",
        work_item_id=f"work:{assignment_id}",
        execution_id=f"execution:{assignment_id}",
        node_id="node:worker",
        pilot_id="pilot:worker",
        candidate=model,
    )


def test_selection_uses_scale_provider_column_and_excludes_zero_likes(tmp_path):
    names = registry(tmp_path)

    claude = allocate(names, "opus-one", candidate("anthropic", "claude-opus-4-1"))
    gpt = allocate(names, "sol-one", candidate("openai", "gpt-5.6-sol"))
    other = allocate(names, "luna-one", candidate("other", "custom-luna"))

    assert (claude.agent_name, claude.scale, claude.name_column) == ("Aki", 2, "日")
    assert (gpt.agent_name, gpt.scale, gpt.name_column) == ("Banner", 3, "英")
    assert (other.agent_name, other.scale, other.name_column) == ("Germen", 1, "羅")
    assert "Forbidden" not in {item.agent_name for item in names.assignments}
    assert "Nagi" not in {item.agent_name for item in names.assignments}


def test_rotation_and_numeric_suffix_are_task_scoped(tmp_path):
    names = registry(tmp_path)
    model = candidate("anthropic", "claude-opus-4-1")

    first = allocate(names, "one", model)
    second = allocate(names, "two", model)

    assert first.agent_name == "Aki"
    assert second.agent_name == "Aki2"
    assert first.agent_name != second.agent_name


def test_assignment_is_restored_without_reusing_a_name(tmp_path):
    source = registry(tmp_path)
    model = candidate("anthropic", "claude-opus-4-1")
    first = allocate(source, "one", model)

    restored = AgentNameRegistry(source.rows)
    restored.restore((first,))
    second = allocate(restored, "two", model)

    assert second.agent_name == "Aki2"


def test_out_of_pipeline_registration_uses_the_same_registry_namespace(tmp_path):
    names = registry(tmp_path)
    model = candidate("anthropic", "claude-opus-4-1")

    first = names.allocate_out_of_pipeline(
        registration_id="registration:commander-child",
        data_space_id="space:personal",
        node_id="node:worker",
        pilot_id="pilot:child-one",
        candidate=model,
    )
    second = allocate(names, "pipeline-work", model)

    assert first.agent_name == "Aki"
    assert second.agent_name == "Aki2"
    assert first.node_id == "node:worker"
    assert first.pilot_id == "pilot:child-one"
    assert names.registration_for_id(first.registration_id) == first
    assert names.is_name_registered(first.agent_name)


def test_dispatch_assigns_and_records_name_in_execution_and_receipt(
    system, tmp_path
):
    kernel, ledger, _, _ = system
    worker = make_node(
        "node:naming-worker", name="Naming worker", kind=NodeKind.UNIT
    )
    kernel.register_node(
        worker, actor_id=OWNER_ID, idempotency_key="node:naming-worker"
    )
    work = WorkItem(
        work_item_id="work:naming-dispatch",
        data_space_id=SPACE_ID,
        title="Naming dispatch",
        description="Verify dispatch attribution.",
        owner_node_id="node:interface",
        delegated_to_node_id=worker.node_id,
        integration_owner_node_id="node:interface",
        parent_work_item_id=None,
        acceptance_criteria=("The dispatched work has an agent name.",),
        route_key="route:naming-test",
        state=WorkState.READY,
        blocking_s3_star_finding_ids=(),
        completion_evidence=None,
    )
    kernel.create_work_item(
        work, actor_id=OWNER_ID, idempotency_key="work:naming-dispatch"
    )
    model = candidate("anthropic", "claude-opus-4-1")
    prior = BenchmarkPrior(
        source="swe-bench",
        benchmark_family="coding",
        version="naming-test",
        sample_count=1,
        harness="deterministic",
        successes=1,
        failures=0,
        log_token_samples=(math.log(10),),
        log_cost_samples=(math.log(0.001),),
        log_latency_samples=(math.log(10),),
    )
    router = BayesianRouter(
        expected_utility_quality_weight=1,
        expected_utility_cost_weight=0,
        expected_utility_latency_weight=0,
    )
    router.register(model, (prior,))
    kernel.route_snapshots["route:naming-test"] = RouteSnapshot(
        snapshot_id="route:naming-test",
        data_space_id=SPACE_ID,
        route_key="route:naming-test",
        evidence_cursor=0,
        candidate_keys=(model.key,),
        production_objective="quality_max",
        state=RouteSnapshotState.PUBLISHED,
        s3_star_approval_event_id="event:naming-s3",
        owner_approval_event_id="event:naming-owner",
    )
    names = registry(tmp_path)

    class Executor:
        def validate_work_candidate(self, selected):
            assert selected == model

        def execute_work(self, **kwargs):
            assert kwargs["agent_name"] == "Aki"
            receipt = PilotHostReceipt(
                receipt_id="receipt:naming-dispatch",
                endpoint="/v1/work-executions",
                idempotency_key=kwargs["idempotency_key"],
                request_sha256="a" * 64,
                status="succeeded",
                candidate_key=model.key,
                requested_model=model.model_snapshot,
                actual_model=model.model_snapshot,
                provider_session_id="session:naming",
                usage={"input_tokens": 1},
                result={
                    "summary": "attributed",
                    "acceptance_results": [],
                    "artifact_refs": [],
                    "event_notes": [],
                    "completed": True,
                },
                error=None,
                created_at=NOW.isoformat(),
                updated_at=NOW.isoformat(),
            )
            return WorkExecutionOutcome(
                receipt=receipt,
                result=WorkExecutionResult.model_validate(receipt.result),
            )

    dispatcher = DependencyAwareDispatcher(
        kernel=kernel,
        router=router,
        evidence_cursor=lambda: 0,
        startup_projection_cursor=ledger.page(0, 100)[-1].cursor,
        model_registry={model.key: model},
        work_executor=Executor(),
        agent_naming_registry=names,
        max_parallelism=1,
    )
    batch = dispatcher.dispatch_ready(
        (
            PilotBinding(
                node_id=worker.node_id,
                pilot_id="pilot:naming",
                pilot_host_id="pilot-host:naming",
            ),
        ),
        actor_id=OWNER_ID,
        idempotency_key="dispatch:naming",
    )
    dispatcher.wait_for_idle()
    dispatcher.close()

    assignment = batch.assignments[0]
    execution = kernel.executions[assignment.execution_id]
    assert assignment.agent_name == "Aki"
    assert execution.agent_name == "Aki"
    events = ledger.page(0, 100)
    assignment_event = next(
        item.event for item in events if item.event.event_type == "agent_name_assigned"
    )
    receipt_event = next(
        item.event
        for item in events
        if item.event.event_type == "pilot_execution_receipt_recorded"
    )
    assert assignment_event.payload["assignment"]["agent_name"] == "Aki"
    assert receipt_event.payload["agent_name"] == "Aki"


# Regression coverage for a production boot failure: Agent_name.csv can carry
# the same name on more than one row (e.g. "Cup" on both a likes=1 "Choko"
# row and a likes=0 "Sakazuki" row). `allocate` only ever picks from the
# likes!=0 pool, so the *assigned* name is always unambiguous at allocation
# time. But `restore` used to match rows on (scale, column, base_name) alone,
# with no eligibility filter, so a likes=0 row sharing a name with a likes=1
# row made the match ambiguous (len != 1) and boot-time restore raised
# InvariantViolation even though the assignment was perfectly valid.

CSV_DUPLICATE_ENGLISH_NAME = """カテゴリ,規模,意味座標,日,英,羅,いいね
器,1,猪口,Choko,Cup,Poculum,1
器,1,杯,Sakazuki,Cup,Calix,0
"""

CSV_DUPLICATE_ELIGIBLE_ENGLISH_NAME = """カテゴリ,規模,意味座標,日,英,羅,いいね
器,2,座標A,FooJa,Bar,BarLatA,1
器,2,座標B,BazJa,Bar,BarLatB,1
"""


def _write_csv(tmp_path: Path, text: str) -> AgentNameRegistry:
    path = tmp_path / "Agent_name.csv"
    path.write_text(text, encoding="utf-8")
    return AgentNameRegistry.from_csv(path)


def test_restore_succeeds_when_a_likes_zero_row_duplicates_the_assigned_name(
    tmp_path,
):
    """Reproduces the production boot crash: restoring 'Cup' must not raise
    InvariantViolation just because a forbidden (likes=0) row also has the
    name 'Cup'."""
    source = _write_csv(tmp_path, CSV_DUPLICATE_ENGLISH_NAME)
    # scale 1 + gpt family ("gpt-" prefix) + "luna" keyword -> column 英, scale 1
    model = candidate("openai", "gpt-5-luna")
    first = allocate(source, "one", model)
    assert first.agent_name == "Cup"
    assert first.base_name == "Cup"

    restored = AgentNameRegistry(source.rows)
    restored.restore((first,))

    assert restored.assignment_for_id(first.assignment_id) == first
    assert restored.is_name_registered("Cup")
    # A subsequent allocation must still see "Cup" as taken.
    second = allocate(restored, "two", model)
    assert second.agent_name != "Cup"


def test_restore_warns_and_keeps_first_csv_row_when_eligible_rows_collide(
    tmp_path, caplog
):
    """If two *eligible* rows share a name (a true CSV duplicate), restore
    must not crash the boot: it logs a warning and deterministically keeps
    the first matching CSV row."""
    source = _write_csv(tmp_path, CSV_DUPLICATE_ELIGIBLE_ENGLISH_NAME)
    restored = AgentNameRegistry(source.rows)
    assignment = _assignment(
        assignment_id="assignment:dup",
        agent_name="Bar",
        base_name="Bar",
        name_column="英",
        scale=2,
    )

    with caplog.at_level(logging.WARNING, logger="vsm.agent_naming"):
        restored.restore((assignment,))

    assert restored.assignment_for_id("assignment:dup") == assignment
    assert restored.is_name_registered("Bar")
    assert any(
        "multiple eligible" in record.message for record in caplog.records
    )


def test_restore_still_raises_when_base_name_is_absent_from_csv(tmp_path):
    """A base_name that matches no CSV row at all is a genuine invariant
    violation and must still fail boot, unlike the duplicate-row cases
    above."""
    source = _write_csv(tmp_path, CSV_DUPLICATE_ENGLISH_NAME)
    restored = AgentNameRegistry(source.rows)
    assignment = _assignment(
        assignment_id="assignment:missing",
        agent_name="Chalice",
        base_name="Chalice",
        name_column="英",
        scale=1,
    )

    with pytest.raises(InvariantViolation, match="not present in Agent_name.csv"):
        restored.restore((assignment,))


def _assignment(
    *, assignment_id: str, agent_name: str, base_name: str, name_column: str, scale: int
):
    return AgentNameAssignment(
        assignment_id=assignment_id,
        data_space_id="space:personal",
        work_item_id="work:naming-restore",
        execution_id="execution:naming-restore",
        node_id="node:worker",
        pilot_id="pilot:worker",
        agent_name=agent_name,
        base_name=base_name,
        suffix=1,
        name_column=name_column,
        scale=scale,
        provider="openai",
        model_candidate_key="model:test",
    )
