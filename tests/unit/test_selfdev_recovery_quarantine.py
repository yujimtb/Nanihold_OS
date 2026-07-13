"""selfdev 起動時の Proposal 単位 integrity quarantine の再現テスト。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vsm.clock import FakeClock
from vsm.selfdev.controller import SelfDevController
from vsm.selfdev.models import ProposalManifest, RunRuntime
from vsm.selfdev.service import SelfDevService
from vsm.selfdev.state_machine import ProposalPhase
from vsm.selfdev.store import SelfDevEventStore
from vsm.web.app import create_app


PROPOSAL_ID = "proposal-e591ebe225714b05a64207ff38ff1a8c"
FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "selfdev_recovery"
PROPOSAL_FIXTURE = FIXTURE_ROOT / PROPOSAL_ID


def _controller(root: Path, store: SelfDevEventStore) -> SelfDevController:
    return SelfDevController(
        repository=root,
        store=store,
        writer_runtime=RunRuntime(
            role="S1_WORKER",
            backend="fake",
            model="fake",
            reasoning_effort="standard",
        ),
        implementation_runner=lambda **_: None,
        gate_runner=lambda **_: None,
        audit_runner=object(),
        consortium=object(),
        worktree_root=root / "worktrees",
        clock=FakeClock(),
    )


def _copy_real_proposal(root: Path) -> None:
    proposal_dir = root / "proposals" / PROPOSAL_ID
    proposal_dir.mkdir(parents=True)
    # 実データは ProposalManifest を canonical bytes で保存していたため、
    # apply_patch で保持した fixture の transport newline だけ除いて原物の bytes に戻す。
    proposal_bytes = (PROPOSAL_FIXTURE / "proposal.json").read_bytes().rstrip(b"\r\n")
    (proposal_dir / "proposal.json").write_bytes(proposal_bytes)
    shutil.copyfile(PROPOSAL_FIXTURE / "workspace.json", proposal_dir / "workspace.json")


async def _append_state(
    store: SelfDevEventStore,
    *,
    from_state: str | None,
    to_state: str,
) -> None:
    reason_codes = {
        (None, ProposalPhase.PROPOSED.value): "proposal_created",
        (ProposalPhase.PROPOSED.value, ProposalPhase.CONSORTIUM_REVIEW.value): "review_started",
        (ProposalPhase.CONSORTIUM_REVIEW.value, ProposalPhase.APPROVED.value): "consortium_approved",
        (ProposalPhase.APPROVED.value, ProposalPhase.WORKSPACE_READY.value): "workspace_ready",
        (ProposalPhase.WORKSPACE_READY.value, ProposalPhase.ABORTED.value): "aborted",
    }
    await store.append(
        "proposal_state_changed",
        {
            "proposal_id": PROPOSAL_ID,
            "from_state": from_state,
            "to_state": to_state,
            "reason_code": reason_codes[(from_state, to_state)],
            "reason": "fixture",
            "related_run_id": None,
            "decision_event_id": None,
            "artifact_refs": (),
        },
        proposal_id=PROPOSAL_ID,
    )


async def _seed(root: Path, *, terminal: bool, with_stale_waiter: bool = False) -> str:
    _copy_real_proposal(root)
    store = SelfDevEventStore(root, clock=FakeClock())
    await store.start()
    try:
        proposal_path = root / "proposals" / PROPOSAL_ID / "proposal.json"
        registered = json.loads((PROPOSAL_FIXTURE / "artifact-record.json").read_text(encoding="utf-8"))
        await _append_state(store, from_state=None, to_state=ProposalPhase.PROPOSED.value)
        await store.append(
            "artifact_created",
            {
                "proposal_id": PROPOSAL_ID,
                "artifact_kind": "proposal_manifest",
                "ref": "proposal.json",
                "sha256": hashlib.sha256(proposal_path.read_bytes()).hexdigest(),
            },
            proposal_id=PROPOSAL_ID,
            schema_version=2,
        )
        await _append_state(
            store,
            from_state=ProposalPhase.PROPOSED.value,
            to_state=ProposalPhase.CONSORTIUM_REVIEW.value,
        )
        await _append_state(
            store,
            from_state=ProposalPhase.CONSORTIUM_REVIEW.value,
            to_state=ProposalPhase.APPROVED.value,
        )
        await _append_state(
            store,
            from_state=ProposalPhase.APPROVED.value,
            to_state=ProposalPhase.WORKSPACE_READY.value,
        )
        await store.append(
            "artifact_created",
            {
                "proposal_id": PROPOSAL_ID,
                "artifact_kind": "workspace_descriptor",
                "ref": registered["artifact_ref"],
                "sha256": registered["registered_sha256"],
            },
            proposal_id=PROPOSAL_ID,
            schema_version=2,
        )
        if with_stale_waiter:
            consortium_id = f"consortium-{PROPOSAL_ID}-initial"
            await store.append(
                "consortium_decided",
                {
                    "consortium_id": consortium_id,
                    "proposal_id": PROPOSAL_ID,
                    "review_kind": "initial",
                    "decision": "APPROVE",
                    "reason": "fixture initial approval",
                    "dissent_summary": "",
                    "conditions": (),
                    "residual_risks": (),
                    "merge_recommendation_reason": None,
                    "dossier_ref": "artifacts/initial-dossier.json",
                    "dossier_sha256": "a" * 64,
                    "human_participated": False,
                    "human_timed_out": True,
                },
                proposal_id=PROPOSAL_ID,
                schema_version=2,
            )
            await store.append(
                "human_review_requested",
                {
                    "proposal_id": PROPOSAL_ID,
                    "consortium_id": consortium_id,
                    "review_id": f"review-{consortium_id}",
                    "review_kind": "initial",
                    "risk_class": "low",
                    "deadline": "2026-07-14T00:00:00.000Z",
                    "approval_required": False,
                },
                proposal_id=PROPOSAL_ID,
                schema_version=2,
            )
        if terminal:
            await _append_state(
                store,
                from_state=ProposalPhase.WORKSPACE_READY.value,
                to_state=ProposalPhase.ABORTED.value,
            )
    finally:
        await store.stop()
    return registered["registered_sha256"]


@pytest.mark.asyncio
async def test_real_proposal_terminal_artifact_is_quarantined_and_projection_excluded(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "selfdev"
    registered_sha = await _seed(root, terminal=True)
    workspace_path = root / "proposals" / PROPOSAL_ID / "workspace.json"
    assert hashlib.sha256(workspace_path.read_bytes()).hexdigest() != registered_sha
    controller = _controller(tmp_path, SelfDevEventStore(root, clock=FakeClock()))

    await controller.start()
    try:
        failures = controller.integrity_failures
        assert len(failures) == 1
        assert failures[0].proposal_id == PROPOSAL_ID
        assert failures[0].disposition == "isolated"
        assert failures[0].failure_kind == "artifact_hash_mismatch"
        assert failures[0].artifact_ref == "workspace.json"
        assert controller.store.replay() == {}
        assert any(
            event.event_type == "proposal_integrity_failed"
            and event.payload["disposition"] == "isolated"
            for event in controller.store.read_events()
        )
    finally:
        await controller.stop()


def test_real_proposal_quarantine_count_is_exposed_by_health(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "selfdev"
    asyncio.run(_seed(root, terminal=True))
    controller = _controller(tmp_path, SelfDevEventStore(root, clock=FakeClock()))
    service = SelfDevService(controller, idle_seconds=0.01)

    with TestClient(create_app(service)) as client:
        health = client.get("/api/selfdev/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "ok"
        assert body["integrity_failed_count"] == 1
        assert body["integrity_failures"][0]["proposal_id"] == PROPOSAL_ID


@pytest.mark.asyncio
async def test_active_proposal_integrity_failure_becomes_needs_human_without_rewrite(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "selfdev"
    await _seed(root, terminal=False)
    workspace_path = root / "proposals" / PROPOSAL_ID / "workspace.json"
    before = workspace_path.read_bytes()
    controller = _controller(tmp_path, SelfDevEventStore(root, clock=FakeClock()))

    await controller.start()
    try:
        projection = controller.store.replay()[PROPOSAL_ID]
        assert projection.aggregate.phase is ProposalPhase.NEEDS_HUMAN
        assert projection.integrity_failed is True
        assert await controller.step() is False
        assert workspace_path.read_bytes() == before
        markers = [
            event
            for event in controller.store.read_events()
            if event.event_type == "proposal_integrity_failed"
        ]
        assert len(markers) == 1
        assert markers[0].payload["disposition"] == "needs_human"
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_active_proposal_integrity_approve_records_resolution_before_resuming(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "selfdev"
    await _seed(root, terminal=False)
    controller = _controller(tmp_path, SelfDevEventStore(root, clock=FakeClock()))

    await controller.start()
    try:
        event = await controller.respond_human(
            PROPOSAL_ID,
            decision="approve",
            response="整合性隔離の再開を明示承認する",
        )
        projection = controller.store.replay()[PROPOSAL_ID]
        assert projection.aggregate.phase is ProposalPhase.APPROVED
        assert projection.integrity_resolved is True
        assert event.event_type == "proposal_state_changed"
        assert any(
            item.event_type == "proposal_integrity_resolved"
            and item.payload["decision"] == "approve"
            for item in controller.store.read_events()
        )
    finally:
        await controller.stop()


def test_real_active_proposal_integrity_reject_routes_away_from_stale_waiter_and_releases_slot(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "selfdev"
    asyncio.run(_seed(root, terminal=False, with_stale_waiter=True))
    controller = _controller(tmp_path, SelfDevEventStore(root, clock=FakeClock()))
    service = SelfDevService(controller, idle_seconds=0.01)

    with TestClient(create_app(service)) as client:
        detail = client.get(f"/api/selfdev/proposals/{PROPOSAL_ID}")
        assert detail.status_code == 200
        current = detail.json()
        assert current["state"] == ProposalPhase.NEEDS_HUMAN.value
        assert current["pending_action"] == "integrity_resolution"

        response = client.post(
            f"/api/selfdev/proposals/{PROPOSAL_ID}/human-decision",
            json={
                "decision": "reject",
                "reason": "整合性隔離された実物Proposalを安全に打ち切る",
                "expected_state_version": current["state_version"],
            },
        )
        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] is True
        assert body["state"] == ProposalPhase.ABORTED.value
        assert body["state_version"] > current["state_version"]
        assert body["event_id"]

        events = controller.store.read_events()
        assert not any(event.event_type == "human_review_responded" for event in events)
        resolutions = [event for event in events if event.event_type == "proposal_integrity_resolved"]
        assert len(resolutions) == 1
        assert resolutions[0].payload["decision"] == "reject"

        proposal = ProposalManifest.model_validate_json(
            (PROPOSAL_FIXTURE / "proposal.json").read_text(encoding="utf-8")
        )
        replacement = proposal.model_copy(update={"id": "proposal-" + "f" * 32})
        created = client.post(
            "/api/selfdev/proposals",
            json={
                "title": replacement.title,
                "motivation": replacement.motivation,
                "scope": [rule.model_dump(mode="json") for rule in replacement.scope],
                "acceptance_criteria": [
                    criterion.model_dump(mode="json")
                    for criterion in replacement.acceptance_criteria
                ],
                "risk_class": replacement.risk_class,
                "budget_estimate": replacement.budget_estimate.model_dump(mode="json"),
                "origin": replacement.origin.model_dump(mode="json"),
                "dependencies": list(replacement.dependencies),
            },
        )
        assert created.status_code == 201


def test_real_active_proposal_integrity_abort_releases_slot_even_without_cleanup_state(tmp_path: Path) -> None:
    root = tmp_path / "runs" / "selfdev"
    asyncio.run(_seed(root, terminal=False))
    controller = _controller(tmp_path, SelfDevEventStore(root, clock=FakeClock()))
    service = SelfDevService(controller, idle_seconds=0.01)

    with TestClient(create_app(service)) as client:
        current = client.get(f"/api/selfdev/proposals/{PROPOSAL_ID}").json()
        response = client.post(
            f"/api/selfdev/proposals/{PROPOSAL_ID}/control",
            json={
                "action": "abort",
                "reason": "整合性隔離された実物Proposalを打ち切る",
                "expected_state_version": current["state_version"],
            },
        )
        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] is True
        assert body["state"] == ProposalPhase.ABORTED.value
        assert body["state_version"] > current["state_version"]
        assert controller.store.replay()[PROPOSAL_ID].aggregate.is_terminal
