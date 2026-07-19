from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import INTERFACE_NODE_ID, OWNER_ID, SPACE_ID
from vsm.interface.models import Conversation
from vsm.pilot.host import PilotHostCoordinator
from vsm.pilot.models import DeviceIdentity
from vsm.routing.bayesian import BayesianRouter, RoutingEvidenceService
from vsm.token_lab.lab import TokenEfficiencyLab, TokenLabEventService
from vsm.web.app import AppState, create_app


def client(system) -> TestClient:
    kernel, ledger, interface, _ = system
    router = BayesianRouter(
        expected_utility_quality_weight=1,
        expected_utility_cost_weight=0,
        expected_utility_latency_weight=0,
    )
    routing_evidence = RoutingEvidenceService(
        router=router,
        ledger=ledger,
        data_space_id=SPACE_ID,
        clock=kernel.clock,
    )
    token_lab = TokenEfficiencyLab()
    token_lab_events = TokenLabEventService(
        lab=token_lab,
        ledger=ledger,
        data_space_id=SPACE_ID,
        clock=kernel.clock,
    )
    state = AppState(
        kernel=kernel,
        interface=interface,
        pilot_hosts=PilotHostCoordinator(
            kernel,
            expected_identity=DeviceIdentity(
                pilot_host_id="pilot-host:test",
                device_id="device:test",
                certificate_sha256="a" * 64,
            ),
        ),
        router=router,
        routing_evidence=routing_evidence,
        token_lab=token_lab,
        token_lab_events=token_lab_events,
        model_registry={},
        api_bearer_token="test-token",
    )
    return TestClient(
        create_app(state, allowed_origins=("http://localhost:5173",))
    )


def auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_new_resource_api_is_complete_and_old_surfaces_do_not_exist(system):
    api = client(system)
    for path in (
        "/api/data-spaces",
        "/api/nodes",
        "/api/work-items",
        "/api/executions",
        "/api/events?after_cursor=0&limit=10",
        "/api/conversations",
        "/api/pilot-hosts",
        "/api/model-registry",
        "/api/route-snapshots",
        "/api/token-lab",
    ):
        response = api.get(path, headers=auth())
        assert response.status_code == 200, (path, response.text)
    for removed in (
        "/api/runs",
        "/api/runs/legacy",
        "/api/chat",
        "/api/chat/messages",
    ):
        assert api.get(removed, headers=auth()).status_code == 404
    assert api.get("/api/nodes").status_code == 401


def test_owner_turn_api_is_one_structured_interface_call(system):
    _, _, _, pilot = system
    api = client(system)
    conversation = Conversation(
        conversation_id="conversation:web",
        data_space_id=SPACE_ID,
        interface_node_id=INTERFACE_NODE_ID,
        owner_id=OWNER_ID,
        provider_session_id=None,
        last_event_cursor=0,
        status="active",
    )
    created = api.post(
        "/api/conversations",
        headers=auth(),
        json={
            "conversation": conversation.model_dump(mode="json"),
            "idempotency_key": "web:create",
        },
    )
    assert created.status_code == 201
    response = api.post(
        "/api/conversations/conversation:web/messages",
        headers=auth(),
        json={
            "text": "いまどう?",
            "idempotency_key": "web:turn",
            "force_new_pilot": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["display_text"] == "accepted:いまどう?"
    assert pilot.calls == 1
    transcript = api.get("/api/conversations", headers=auth()).json()
    assert transcript["messages"]["conversation:web"][0]["display_text"] == "いまどう?"


def test_unregistered_pilot_host_identity_is_rejected(system):
    api = client(system)
    response = api.post(
        "/api/pilot-hosts/connect",
        headers=auth(),
        json={
            "identity": {
                "pilot_host_id": "pilot-host:other",
                "device_id": "device:other",
                "certificate_sha256": "b" * 64,
            },
            "acknowledged_cursor": 0,
            "connected_at": "2026-07-19T12:00:00Z",
        },
    )
    assert response.status_code == 409
