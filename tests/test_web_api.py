from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import INTERFACE_NODE_ID, OWNER_ID, SPACE_ID
from vsm.dispatcher import DependencyAwareDispatcher
from vsm.interface.models import Conversation, SurfaceBinding
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
        authorized_device_ids=frozenset({"device:test"}),
        dispatcher=DependencyAwareDispatcher(
            kernel=kernel,
            router=router,
            evidence_cursor=lambda: routing_evidence.evidence_cursor,
        ),
        owner_session_lifetime_seconds=3600,
        history_reader=object(),
        history_max_result_bytes=4096,
    )
    return TestClient(
        create_app(state, allowed_origins=("http://localhost:5173",)),
        base_url="https://testserver",
    )


def auth() -> dict[str, str]:
    return {
        "Authorization": "Bearer test-token",
        "X-Nanihold-Device-Id": "device:test",
    }


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
        "/api/history/imports",
        "/api/history/sessions",
        "/api/reorientation",
        "/api/activation/status",
    ):
        response = api.get(path, headers=auth())
        assert response.status_code == 200, (path, response.text)
    for removed in (
        "/api/runs",
        "/api/runs/legacy",
        "/api/chat",
        "/api/chat/messages",
        "/api/conversations/conversation:legacy/messages",
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
        title="Web",
    )
    surface_binding = SurfaceBinding(
        binding_id="binding:web",
        conversation_id="conversation:web",
        surface="slack",
        source_session_id="slack-thread",
        channel_id="channel-one",
        device_id="device:test",
    )
    created = api.post(
        "/api/conversations",
        headers=auth(),
        json={
            "conversation": conversation.model_dump(mode="json"),
            "surface_binding": surface_binding.model_dump(mode="json"),
            "idempotency_key": "web:create",
        },
    )
    assert created.status_code == 201
    response = api.post(
        "/api/conversations/conversation:web/actions",
        headers=auth(),
        json={
            "action_id": "action:web-turn",
            "idempotency_key": "web:turn",
            "kind": "owner_message",
            "text": "いまどう?",
            "source": {
                "surface": "slack",
                "source_session_id": "slack-thread",
                "source_message_id": "source-message-one",
                "author_id": OWNER_ID,
                "channel_id": "channel-one",
                "occurred_at": "2026-07-19T12:00:00Z",
            },
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["interface_message"]["display_text"] == "accepted:いまどう?"
    assert pilot.calls == 1
    transcript = api.get("/api/conversations", headers=auth()).json()
    assert transcript["messages"]["conversation:web"][0]["display_text"] == "いまどう?"
    reconciled = api.get(
        "/api/conversations/conversation:web/actions/action:web-turn",
        headers=auth(),
    )
    assert reconciled.status_code == 200
    assert reconciled.json() == response.json()


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


def test_cookie_cors_preflight_is_explicit(system):
    api = client(system)
    response = api.options(
        "/api/conversations",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "content-type,authorization,x-nanihold-device-id"
            ),
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "*" not in response.headers["access-control-allow-headers"]


def test_owner_bootstrap_exchanges_once_for_strict_cookie(system):
    kernel, _, _, _ = system
    api = client(system)
    grant = kernel.owner_bootstrap.issue(
        base_url="https://nanihold.local",
        lifetime_seconds=60,
        idempotency_key="web-bootstrap:issue",
    )
    exchanged = api.post(
        "/api/owner-bootstrap/exchange",
        json={
            "code": grant.code,
            "device_id": "owner-browser",
            "idempotency_key": "web-bootstrap:exchange",
        },
    )
    assert exchanged.status_code == 200
    cookie = exchanged.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie
    token = cookie.split(";", 1)[0].split("=", 1)[1]
    status_response = api.get(
        "/api/activation/status",
        headers={"Cookie": f"nanihold_owner_session={token}"},
    )
    assert status_response.status_code == 200
    reused = api.post(
        "/api/owner-bootstrap/exchange",
        json={
            "code": grant.code,
            "device_id": "owner-browser",
            "idempotency_key": "web-bootstrap:reuse",
        },
    )
    assert reused.status_code == 409
