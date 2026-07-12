from __future__ import annotations

from pathlib import Path

import pytest

from vsm.clock import FakeClock
from vsm.eventlog.schema import validate_event_payload
from vsm.selfdev.models import (
    AcceptanceCriterion,
    ActorRef,
    BudgetEstimate,
    PathRule,
    ProposalManifest,
    RunRuntime,
    proposal_to_run_manifest,
)
from vsm.selfdev.projection import replay_proposal_events
from vsm.selfdev.state_machine import PauseCause, PauseKind, ProposalAggregate, ProposalPhase
from vsm.selfdev.store import SelfDevEventStore


def _proposal() -> ProposalManifest:
    return ProposalManifest(
        id="proposal-" + "a" * 32,
        title="Wave 1",
        motivation="今必要な基盤を追加する",
        scope=(PathRule(path="docs", kind="tree"),),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="AC-1",
                statement="文書に forbidden がない",
                verifier={"kind": "file_not_contains", "path": "docs/readme.md", "literal": "forbidden"},
            ),
        ),
        risk_class="normal",
        budget_estimate=BudgetEstimate(tokens=10, active_wall_clock_seconds=20),
        origin={"kind": "conversation", "conversation_id": "conv-1", "decision_ref": "decision-1"},
        created_at="2026-07-13T00:00:00.000Z",
        created_by=ActorRef(actor_type="human", actor_id="operator"),
    )


def test_proposal_manifest_rejects_scope_outside_verifier() -> None:
    with pytest.raises(ValueError, match="scope 外"):
        ProposalManifest(
            **{
                **_proposal().model_dump(mode="python"),
                "acceptance_criteria": (
                    {
                        "id": "AC-1",
                        "statement": "bad",
                        "verifier": {"kind": "path_exists", "path": "vsm/secret.py"},
                    },
                ),
            }
        )


def test_proposal_run_mapping_derives_branch_from_proposal() -> None:
    manifest = proposal_to_run_manifest(
        _proposal(),
        repository=Path("."),
        base_sha="a" * 40,
        worktree_path=Path("worktree"),
        initial_decision_event_id="event-1",
        writer_runtime=RunRuntime(
            role="S1_WORKER", backend="codex", model="gpt-5.6-luna", reasoning_effort="xhigh"
        ),
        run_id="run-" + "b" * 32,
    )
    assert manifest.branch == "selfdev/proposal-" + "a" * 32
    assert manifest.proposal_id == _proposal().id
    assert manifest.required_gates == ("g1", "g2", "g3", "g4")


@pytest.mark.asyncio
async def test_selfdev_store_durable_replay_and_stream_version(tmp_path: Path) -> None:
    proposal = _proposal()
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=FakeClock())
    await store.start()
    try:
        first = await store.append(
            "proposal_state_changed",
            {
                "proposal_id": proposal.id,
                "from_state": None,
                "to_state": "PROPOSED",
                "reason_code": "proposal_created",
                "reason": "created",
                "related_run_id": None,
                "decision_event_id": None,
                "artifact_refs": [],
            },
            proposal_id=proposal.id,
        )
        second = await store.append(
            "proposal_state_changed",
            {
                "proposal_id": proposal.id,
                "from_state": "PROPOSED",
                "to_state": "CONSORTIUM_REVIEW",
                "reason_code": "review_started",
                "reason": "review",
                "related_run_id": None,
                "decision_event_id": None,
                "artifact_refs": [],
            },
            proposal_id=proposal.id,
            expected_stream_version=1,
        )
        assert first.stream_version == 1
        assert second.stream_version == 2
        projection = store.projection(proposal.id)
        assert projection is not None
        assert projection.aggregate.phase is ProposalPhase.CONSORTIUM_REVIEW
    finally:
        await store.stop()


def test_pause_causes_are_orthogonal() -> None:
    aggregate = ProposalAggregate()
    aggregate = aggregate.pause(
        PauseCause(
            pause_id="pause-human",
            kind=PauseKind.SUSPEND,
            actor_type="human",
            actor_id="operator",
            source_event_id="event-1",
            reason="確認待ち",
        )
    )
    aggregate = aggregate.pause(
        PauseCause(
            pause_id="pause-quota",
            kind=PauseKind.QUOTA_WAIT,
            actor_type="controller",
            actor_id="quota",
            pool_id="codex-pro",
            reset_at=__import__("datetime").datetime(2026, 7, 13, tzinfo=__import__("datetime").timezone.utc),
            source_event_id="event-2",
            reason="quota",
        )
    )
    assert {cause.kind for cause in aggregate.pause_causes} == {PauseKind.SUSPEND, PauseKind.QUOTA_WAIT}


def test_unknown_selfdev_event_schema_fails_fast() -> None:
    with pytest.raises(ValueError, match="unknown event schema"):
        validate_event_payload(
            "proposal_state_changed",
            {"proposal_id": "proposal-" + "a" * 32},
            schema_version=99,
        )
