"""Wave 4 selfdev REST surface tests with a deterministic controller seam."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from vsm.agents.backends.fake import FakeAgentRuntime
from vsm.clock import FakeClock
from vsm.selfdev.models import (
    AcceptanceCriterion,
    ActorRef,
    BudgetEstimate,
    PathRule,
    ProposalManifest,
    ConversationOrigin,
)
from vsm.selfdev.store import SelfDevEventStore
from vsm.web.app import create_app


def _proposal() -> ProposalManifest:
    return ProposalManifest(
        id="proposal-" + "a" * 32,
        title="API の決定論テスト",
        motivation="REST projection の契約を確認する",
        scope=(PathRule(path="docs/api.md", kind="file"),),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="AC-1",
                statement="対象ファイルが存在する",
                verifier={"kind": "path_exists", "path": "docs/api.md"},
            ),
        ),
        risk_class="normal",
        budget_estimate=BudgetEstimate(tokens=100, active_wall_clock_seconds=60),
        origin=ConversationOrigin(kind="conversation", decision_ref="api-test", conversation_id="chat-api"),
        created_at="2026-07-13T00:00:00.000Z",
        created_by=ActorRef(actor_type="human", actor_id="test"),
    )


def _create_body(proposal: ProposalManifest) -> dict:
    return {
        "title": proposal.title,
        "motivation": proposal.motivation,
        "scope": [rule.model_dump(mode="json") for rule in proposal.scope],
        "acceptance_criteria": [criterion.model_dump(mode="json") for criterion in proposal.acceptance_criteria],
        "risk_class": proposal.risk_class,
        "budget_estimate": proposal.budget_estimate.model_dump(mode="json"),
        "origin": proposal.origin.model_dump(mode="json"),
        "dependencies": [],
    }


class FakeController:
    def __init__(self, root: Path) -> None:
        self.store = SelfDevEventStore(root, clock=FakeClock())
        self._started = False
        self.runtime = FakeAgentRuntime(response="決定論的 fake response")
        self._budget_actual: dict[str, int] = {}

    async def start(self) -> None:
        await self.store.start()
        self._started = True

    async def stop(self) -> None:
        await self.store.stop()
        self._started = False

    async def submit_proposal(self, proposal: ProposalManifest) -> str:
        self.store.layout.write_proposal_manifest(proposal)
        await self.store.append(
            "proposal_state_changed",
            {
                "proposal_id": proposal.id,
                "from_state": None,
                "to_state": "PROPOSED",
                "reason_code": "proposal_created",
                "reason": "test",
                "related_run_id": None,
                "decision_event_id": None,
                "artifact_refs": (),
            },
            proposal_id=proposal.id,
        )
        await self.store.append(
            "artifact_created",
            {
                "proposal_id": proposal.id,
                "artifact_kind": "proposal_manifest",
                "ref": "proposal.json",
                "sha256": proposal.sha256(),
            },
            proposal_id=proposal.id,
            schema_version=2,
        )
        return proposal.id


class FakeService:
    def __init__(self, controller: FakeController) -> None:
        self.controller = controller

    @property
    def healthy(self) -> bool:
        return self.controller._started

    @property
    def fatal(self):
        return None

    async def start(self) -> None:
        await self.controller.start()

    async def stop(self) -> None:
        await self.controller.stop()


def test_selfdev_create_list_detail_and_health(tmp_path: Path) -> None:
    controller = FakeController(tmp_path / "runs" / "selfdev")
    service = FakeService(controller)
    app = create_app(service)
    with TestClient(app) as client:
        created = client.post(
            "/api/selfdev/proposals",
            json={
                "title": "Web から作成",
                "motivation": "一覧表示の決定論確認",
                "scope": [{"path": "docs/api.md", "kind": "file"}],
                "acceptance_criteria": [{
                    "id": "AC-1", "statement": "存在する",
                    "verifier": {"kind": "path_exists", "path": "docs/api.md"},
                }],
                "risk_class": "normal",
                "budget_estimate": {"tokens": 10, "active_wall_clock_seconds": 5, "pool_quota": []},
                "origin": {"kind": "conversation", "decision_ref": "web", "conversation_id": "chat-1"},
                "dependencies": [],
            },
        )
        assert created.status_code == 201
        proposal_id = created.json()["proposal_id"]
        assert proposal_id.startswith("proposal-")
        listed = client.get("/api/selfdev/proposals").json()
        assert listed["items"][0]["state"] == "PROPOSED"
        detail = client.get(f"/api/selfdev/proposals/{proposal_id}")
        assert detail.status_code == 200
        assert detail.json()["proposal"]["title"] == "Web から作成"
        assert detail.json()["transitions"][0]["transition"]["to_state"] == "PROPOSED"
        assert client.get("/api/selfdev/health").json()["status"] == "ok"


def test_selfdev_api_stale_mutation_and_artifact_traversal(tmp_path: Path) -> None:
    controller = FakeController(tmp_path / "runs" / "selfdev")
    proposal = _proposal()
    service = FakeService(controller)
    app = create_app(service)
    with TestClient(app) as client:
        created = client.post("/api/selfdev/proposals", json=_create_body(proposal))
        assert created.status_code == 201
        proposal_id = created.json()["proposal_id"]
        stale = client.post(
            f"/api/selfdev/proposals/{proposal_id}/control",
            json={"action": "abort", "reason": "stale", "expected_state_version": 99},
        )
        assert stale.status_code == 409
        assert client.get(f"/api/selfdev/proposals/{proposal_id}/artifacts/../proposal.json").status_code == 404


def test_selfdev_events_are_sse_and_cli_contract_is_not_leaked(tmp_path: Path) -> None:
    controller = FakeController(tmp_path / "runs" / "selfdev")
    service = FakeService(controller)
    app = create_app(service)
    with TestClient(app) as client:
        created = client.post("/api/selfdev/proposals", json=_create_body(_proposal()))
        assert created.status_code == 201
        proposal_id = created.json()["proposal_id"]
        response = client.get(f"/api/selfdev/proposals/{proposal_id}/events")
        assert response.status_code == 200
        assert "event: selfdev" in response.text
        assert "proposal_state_changed" in response.text
        assert "/approvals" not in response.text


def test_selfdev_api_returns_503_without_controller_and_422_for_protected_semantics(tmp_path: Path) -> None:
    app = create_app(None)
    with TestClient(app) as client:
        response = client.post("/api/selfdev/proposals", json=_create_body(_proposal()))
        assert response.status_code == 503

    controller = FakeController(tmp_path / "runs" / "selfdev")
    app = create_app(FakeService(controller))
    body = _create_body(_proposal())
    body["scope"] = [{"path": ".github/settings.yml", "kind": "file"}]
    with TestClient(app) as client:
        response = client.post("/api/selfdev/proposals", json=body)
        assert response.status_code == 422
