from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from conftest import INTERFACE_NODE_ID, NOW, OWNER_ID, SPACE_ID
from vsm.activation.models import (
    ActivationState,
    EvidenceCitation,
    HistoryImportReceipt,
    HistorySourceKind,
    HistorySourceManifest,
    ReorientationAssessment,
)
from vsm.dispatcher import DependencyAwareDispatcher
from vsm.errors import InvariantViolation
from vsm.interface.models import Conversation, SurfaceBinding
from vsm.pilot.host import PilotHostCoordinator
from vsm.pilot.models import DeviceIdentity
from vsm.routing.bayesian import BayesianRouter, RoutingEvidenceService
from vsm.token_lab.lab import TokenEfficiencyLab, TokenLabEventService
from vsm.web.app import AppState, create_app


def client(
    system, *, reorientation_service=None, reorientation_max_tool_rounds=None
) -> TestClient:
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
        reorientation_service=reorientation_service,
        reorientation_max_tool_rounds=reorientation_max_tool_rounds,
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


class FailingReorientationService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def execute(self, **_kwargs) -> None:
        raise self.error


def prepare_history_imported_state(kernel) -> None:
    kernel.activation.state = ActivationState.HISTORY_IMPORTED
    sources = tuple(
        HistorySourceManifest(
            source_id=f"{kind.value}:test",
            source_kind=kind,
            ownership="personal",
            owner_id=OWNER_ID,
            record_count=0,
            raw_bytes=0,
            digest_sha256=hashlib.sha256(kind.value.encode("utf-8")).hexdigest(),
            cutover_cursor=f"cursor:{kind.value}",
        )
        for kind in HistorySourceKind
    )
    kernel.activation.import_receipt = HistoryImportReceipt(
        schema="schema:history-activation-handoff",
        schema_version="1.0.0",
        inventory_id="history-import:web-test",
        data_space_id=SPACE_ID,
        manifest_digest=hashlib.sha256(b"web-history-import").hexdigest(),
        record_count=0,
        raw_bytes=0,
        cross_source_overlap_identities=0,
        sources=sources,
        session_count=0,
        sessions=(),
        session_index_ref="history-projection:sessions:sha256:" + "a" * 64,
        open_commitments_ref="history-projection:commitments:sha256:" + "b" * 64,
        current_state_ref="history-projection:state:sha256:" + "c" * 64,
    )


def test_reorientation_short_invariant_failure_is_visible_in_activation_status(system):
    kernel, _, _, _ = system
    prepare_history_imported_state(kernel)
    api = client(
        system,
        reorientation_service=FailingReorientationService(
            InvariantViolation("phase validation failed")
        ),
        reorientation_max_tool_rounds=1,
    )

    started = api.post(
        "/api/reorientation/start",
        headers=auth(),
        json={"actor_id": OWNER_ID, "idempotency_key": "web:reorientation:short"},
    )

    assert started.status_code == 202, started.text
    status = api.get("/api/activation/status", headers=auth())
    assert status.status_code == 200
    assert status.json()["reorientation_error"] == (
        "InvariantViolation: phase validation failed"
    )


def test_reorientation_long_failure_records_only_digest_in_activation_status(system):
    kernel, _, _, _ = system
    prepare_history_imported_state(kernel)
    secret_body = "secret-not-for-projection-" + "x" * 600
    api = client(
        system,
        reorientation_service=FailingReorientationService(
            InvariantViolation(secret_body)
        ),
        reorientation_max_tool_rounds=1,
    )

    started = api.post(
        "/api/reorientation/start",
        headers=auth(),
        json={"actor_id": OWNER_ID, "idempotency_key": "web:reorientation:long"},
    )

    assert started.status_code == 202, started.text
    error_code = api.get("/api/activation/status", headers=auth()).json()[
        "reorientation_error"
    ]
    assert error_code == "InvariantViolation: sha256:" + hashlib.sha256(
        secret_body.encode("utf-8")
    ).hexdigest()
    assert secret_body not in error_code
    assert "\n" not in error_code


def test_reorientation_revision_returns_to_read_only_without_execution_or_effect(system):
    kernel, ledger, _, _ = system
    assessment = ReorientationAssessment(
        assessment_id="assessment:web-revision",
        import_id="history-import:web-revision",
        conversation_id="conversation:web-revision",
        generated_at=NOW,
        understanding="The assessment requires an owner correction.",
        active_missions=(),
        decisions_and_constraints=(),
        open_commitment_ids=(),
        unknowns=(),
        resume_work_item_ids=("work:web-revision",),
        covered_session_index_ref="history-index:web-revision",
        covered_session_count=0,
        history_cursor=1,
        current_state_cursor=1,
        citations=(
            EvidenceCitation(
                claim_ref="understanding",
                evidence_ref="event:web-revision",
            ),
        ),
    )
    kernel.activation.state = ActivationState.AWAITING_OWNER_CONFIRMATION
    kernel.activation.assessment = assessment
    kernel.activation.reorientation_error = "prior-error"
    kernel.activation.reorientation_provider_session_id = "provider:checkpoint"
    kernel.activation.reorientation_pilot_calls = 2
    kernel.activation.reorientation_input_tokens = 120
    kernel.activation.reorientation_output_tokens = 30
    api = client(system)

    response = api.post(
        "/api/reorientation/revision",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:reorientation:revision",
            "reason_code": "owner_correction",
            "requested_by": "owner",
        },
    )

    assert response.status_code == 200, response.text
    status = response.json()
    assert status["state"] == "REORIENTATION_ONLY"
    assert status["assessment"] is None
    assert status["reorientation_error"] is None
    assert status["reorientation_pilot_calls"] == 2
    assert status["reorientation_input_tokens"] == 120
    assert status["reorientation_output_tokens"] == 30
    assert kernel.activation.reorientation_provider_session_id == "provider:checkpoint"
    assert kernel.executions == {}
    assert kernel.effect_leases == {}
    revision_event = ledger.page(0, 100)[-1].event
    assert revision_event.event_type == (
        "reorientation_assessment_revision_requested"
    )
    assert revision_event.actor_type == "human"
    assert revision_event.payload == {
        "prior_assessment_id": "assessment:web-revision",
        "reason_code": "owner_correction",
        "state": "REORIENTATION_ONLY",
    }


def test_reorientation_revision_rejects_unknown_reason_code(system):
    kernel, _, _, _ = system
    kernel.activation.state = ActivationState.AWAITING_OWNER_CONFIRMATION
    kernel.activation.assessment = ReorientationAssessment(
        assessment_id="assessment:web-invalid-revision",
        import_id="history-import:web-invalid-revision",
        conversation_id="conversation:web-invalid-revision",
        generated_at=NOW,
        understanding="A valid assessment.",
        active_missions=(),
        decisions_and_constraints=(),
        open_commitment_ids=(),
        unknowns=(),
        resume_work_item_ids=(),
        covered_session_index_ref="history-index:web-invalid-revision",
        covered_session_count=0,
        history_cursor=1,
        current_state_cursor=1,
        citations=(
            EvidenceCitation(
                claim_ref="understanding",
                evidence_ref="event:web-invalid-revision",
            ),
        ),
    )
    api = client(system)

    response = api.post(
        "/api/reorientation/revision",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:reorientation:invalid-revision",
            "reason_code": "free_form_reason",
            "requested_by": "owner",
        },
    )

    assert response.status_code == 422
    assert kernel.activation.state is ActivationState.AWAITING_OWNER_CONFIRMATION


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
