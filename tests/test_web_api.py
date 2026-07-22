from __future__ import annotations

import hashlib
import math

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
from vsm.agent_naming import AgentNameRegistry, AgentNameRow
from vsm.dispatcher import (
    DependencyAwareDispatcher,
    DispatchAssignment,
    DispatchBatch,
)
from vsm.errors import InvariantViolation
from vsm.interface.models import Conversation, SurfaceBinding
from vsm.pilot.host import PilotHostCoordinator
from vsm.kernel.models import (
    Execution,
    ExecutionState,
    RouteSnapshot,
    RouteSnapshotState,
    WorkItem,
    WorkState,
)
from vsm.pilot.models import DeviceIdentity, ModelCandidate
from vsm.routing.bayesian import (
    BayesianRouter,
    BenchmarkPrior,
    RoutingEvidenceService,
)
from vsm.token_lab.lab import TokenEfficiencyLab, TokenLabEventService
from vsm.web.app import AppState, create_app


def client(
    system,
    *,
    reorientation_service=None,
    reorientation_max_tool_rounds=None,
    model_candidates: tuple[ModelCandidate, ...] = (),
    dispatcher=None,
    coding_pilot_id: str | None = None,
    owner_auth_disabled: bool = False,
    agent_name_registry: AgentNameRegistry | None = None,
) -> TestClient:
    kernel, ledger, interface, _ = system
    router = BayesianRouter(
        expected_utility_quality_weight=1,
        expected_utility_cost_weight=0,
        expected_utility_latency_weight=0,
    )
    prior = BenchmarkPrior(
        source="local-verification",
        benchmark_family="interface",
        version="test",
        sample_count=1,
        harness="deterministic",
        successes=1,
        failures=0,
        log_token_samples=(math.log(10),),
        log_cost_samples=(math.log(0.001),),
        log_latency_samples=(math.log(10),),
    )
    for candidate in model_candidates:
        router.register(candidate, (prior,))
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
        model_registry={
            candidate.key: candidate for candidate in model_candidates
        },
        api_bearer_token="test-token",
        authorized_device_ids=frozenset({"device:test"}),
        dispatcher=(
            dispatcher
            if dispatcher is not None
            else DependencyAwareDispatcher(
                kernel=kernel,
                router=router,
                evidence_cursor=lambda: routing_evidence.evidence_cursor,
                startup_projection_cursor=0,
            )
        ),
        owner_session_lifetime_seconds=3600,
        history_reader=object(),
        history_max_result_bytes=4096,
        agent_name_registry=agent_name_registry,
        reorientation_service=reorientation_service,
        reorientation_max_tool_rounds=reorientation_max_tool_rounds,
        coding_pilot_id=coding_pilot_id,
        owner_auth_disabled=owner_auth_disabled,
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


class RecordingDispatcher:
    def __init__(self) -> None:
        self.preflight_calls = []
        self.dispatch_calls = []

    def preflight_ready(self, bindings, **kwargs) -> None:
        self.preflight_calls.append((bindings, kwargs))

    def dispatch_ready(self, bindings, **kwargs) -> DispatchBatch:
        self.dispatch_calls.append((bindings, kwargs))
        work_item_id = next(iter(kwargs["allowed_work_item_ids"]))
        return DispatchBatch(
            assignments=(
                DispatchAssignment(
                    work_item_id=work_item_id,
                    execution_id="execution:web-dispatch",
                    pilot_id=bindings[0].pilot_id,
                    pilot_host_id=bindings[0].pilot_host_id,
                    model_candidate_key="candidate:web-dispatch",
                ),
            ),
            parallelism=1,
            model_calls=1,
        )


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
        "/api/agent-messages",
        "/api/agent-identities",
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


def test_out_of_pipeline_agent_identity_is_issued_by_the_registry(system):
    kernel, _, _, _ = system
    names = AgentNameRegistry(
        [
            AgentNameRow(
                category="居",
                scale=2,
                semantic_coordinate="甲",
                japanese_name="Kaba",
                english_name="Autumn",
                latin_name="Autumnus",
                likes="1",
            ),
            AgentNameRow(
                category="居",
                scale=2,
                semantic_coordinate="乙",
                japanese_name="Toki",
                english_name="Spring",
                latin_name="Ver",
                likes="1",
            ),
        ]
    )
    candidate = ModelCandidate(
        adapter="test",
        adapter_version="1",
        provider="anthropic",
        selection="exact",
        model_snapshot="claude-opus-4-1",
        effort="high",
        toolset=(),
        sandbox_fingerprint="sandbox:test",
        environment_fingerprint="environment:test",
    )
    api = client(system, agent_name_registry=names)

    response = api.post(
        "/api/agent-identities",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:agent-identity:register",
            "registration_id": "registration:web-child",
            "node_id": INTERFACE_NODE_ID,
            "pilot_id": "pilot:web-child",
            "candidate": candidate.model_dump(mode="json", exclude={"key"}),
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["agent_name"] == "Kaba"
    assert kernel.agent_name_registrations["registration:web-child"].pilot_id == (
        "pilot:web-child"
    )
    second_registration = api.post(
        "/api/agent-identities",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:agent-identity:register-second",
            "registration_id": "registration:web-child-second",
            "node_id": INTERFACE_NODE_ID,
            "pilot_id": "pilot:web-child-second",
            "candidate": candidate.model_dump(mode="json", exclude={"key"}),
        },
    )
    assert second_registration.status_code == 201
    work_item = WorkItem(
        work_item_id="work:web-agent-message",
        data_space_id=SPACE_ID,
        title="Agent message context",
        description="Record one internal message with durable context.",
        owner_node_id=INTERFACE_NODE_ID,
        delegated_to_node_id=INTERFACE_NODE_ID,
        integration_owner_node_id=INTERFACE_NODE_ID,
        parent_work_item_id=None,
        acceptance_criteria=("The internal message is audited.",),
        route_key="route:web-agent-message",
        state=WorkState.READY,
        blocking_s3_star_finding_ids=(),
        completion_evidence=None,
    )
    kernel.create_work_item(
        work_item, actor_id=OWNER_ID, idempotency_key="work:web-agent-message"
    )
    execution = Execution(
        execution_id="execution:web-agent-message",
        data_space_id=SPACE_ID,
        node_id=INTERFACE_NODE_ID,
        work_item_id=work_item.work_item_id,
        pilot_id="pilot:web-child",
        model_candidate_key=candidate.key,
        state=ExecutionState.REQUESTED,
        provider_session_id=None,
        pilot_host_id="pilot-host:web-agent-message",
        pause_reason=None,
    )
    kernel.create_execution(
        execution, actor_id=OWNER_ID, idempotency_key="execution:web-agent-message"
    )
    sent = api.post(
        "/api/agent-messages",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:agent-message:send",
            "data_space_id": SPACE_ID,
            "source_instance_id": "nanihold:web-test",
            "sender_agent_name": "Kaba",
            "recipient_agent_name": "Toki",
            "source_message_id": "message:web-agent-message",
            "body": "Please coordinate.",
            "related_work_item_id": work_item.work_item_id,
            "related_execution_id": execution.execution_id,
        },
    )
    assert sent.status_code == 201, sent.text
    assert api.get(
        "/api/agent-messages?recipient_agent_name=Toki", headers=auth()
    ).json()["items"][0]["sender_agent_name"] == "Kaba"
    assert api.get("/api/notifications", headers=auth()).json()["items"][-1][
        "owner_visible"
    ] is True
    identities = api.get("/api/agent-identities", headers=auth())
    assert identities.status_code == 200
    assert {item["agent_name"] for item in identities.json()["registrations"]} == {
        "Kaba",
        "Toki",
    }


def test_auth_enabled_by_default_rejects_unauthenticated_requests(system):
    # Default (owner_auth_disabled omitted / False) keeps authentication on.
    api = client(system)
    assert api.get("/api/activation/status").status_code == 401
    assert api.get("/api/nodes").status_code == 401
    assert api.get("/api/activation/status", headers=auth()).status_code == 200


def test_owner_auth_disabled_bypasses_all_authentication(system):
    api = client(system, owner_auth_disabled=True)
    # No Authorization header, no device id, no owner session cookie required.
    for path in (
        "/api/activation/status",
        "/api/nodes",
        "/api/data-spaces",
        "/api/conversations",
    ):
        response = api.get(path)
        assert response.status_code == 200, (path, response.text)


def test_owner_auth_disabled_keeps_bootstrap_endpoints_from_500(system):
    api = client(system, owner_auth_disabled=True)
    # Issue is protected by authorize (now bypassed): a valid origin succeeds and
    # an untrusted origin fails cleanly with 409 rather than 500.
    issued = api.post(
        "/api/owner-bootstrap/issues",
        json={
            "base_url": "http://localhost:5173",
            "lifetime_seconds": 60,
            "idempotency_key": "auth-disabled:issue",
        },
    )
    assert issued.status_code == 201, issued.text
    rejected = api.post(
        "/api/owner-bootstrap/issues",
        json={
            "base_url": "https://untrusted.example",
            "lifetime_seconds": 60,
            "idempotency_key": "auth-disabled:untrusted",
        },
    )
    assert rejected.status_code == 409, rejected.text
    # Exchange with an unknown code fails cleanly (not 500) as well.
    exchanged = api.post(
        "/api/owner-bootstrap/exchange",
        json={
            "code": "code:does-not-exist",
            "device_id": "owner-browser",
            "idempotency_key": "auth-disabled:exchange",
        },
    )
    assert exchanged.status_code != 500, exchanged.text


def test_active_work_item_can_be_explicitly_dispatched(system):
    kernel, _, _, _ = system
    kernel.activation.state = ActivationState.ACTIVE
    work_item = WorkItem(
        work_item_id="work:web-dispatch",
        data_space_id=SPACE_ID,
        title="Dispatch through the typed API",
        description="Resume one real ready WorkItem.",
        owner_node_id=INTERFACE_NODE_ID,
        delegated_to_node_id=INTERFACE_NODE_ID,
        integration_owner_node_id=INTERFACE_NODE_ID,
        parent_work_item_id=None,
        acceptance_criteria=("typed dispatch is recorded",),
        route_key="coding_s1",
        state=WorkState.READY,
        blocking_s3_star_finding_ids=(),
        completion_evidence=None,
    )
    kernel.create_work_item(
        work_item,
        actor_id=OWNER_ID,
        idempotency_key="web:dispatch:create-work",
    )
    dispatcher = RecordingDispatcher()
    api = client(
        system,
        dispatcher=dispatcher,
        coding_pilot_id="pilot:coding-s1",
    )

    response = api.post(
        f"/api/work-items/{work_item.work_item_id}/dispatches",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:dispatch:start",
        },
    )

    assert response.status_code == 202, response.text
    assert response.json()["assignments"][0]["work_item_id"] == work_item.work_item_id
    assert response.json()["model_calls"] == 1
    preflight_bindings, preflight_options = dispatcher.preflight_calls[0]
    assert preflight_bindings[0].node_id == INTERFACE_NODE_ID
    assert preflight_bindings[0].pilot_id == "pilot:coding-s1"
    assert preflight_options["allowed_work_item_ids"] == frozenset(
        {work_item.work_item_id}
    )
    dispatch_bindings, dispatch_options = dispatcher.dispatch_calls[0]
    assert dispatch_bindings == preflight_bindings
    assert dispatch_options["idempotency_key"] == "web:dispatch:start"


def test_route_snapshot_retirement_requires_typed_approved_replacement(system):
    kernel, _, _, _ = system
    candidate = ModelCandidate(
        adapter="fake",
        adapter_version="1.0",
        provider="test",
        selection="exact",
        model_snapshot="fake-model",
        effort="low",
        toolset=("filesystem",),
        sandbox_fingerprint="sandbox:test",
        environment_fingerprint="environment:test",
    )
    api = client(system, model_candidates=(candidate,))

    def snapshot(
        snapshot_id: str,
        candidate_key: str,
        route_key: str = "coding_s1",
    ) -> RouteSnapshot:
        return RouteSnapshot(
            snapshot_id=snapshot_id,
            data_space_id=SPACE_ID,
            route_key=route_key,
            evidence_cursor=0,
            candidate_keys=(candidate_key,),
            production_objective="quality_max",
            state=RouteSnapshotState.DRAFT,
            s3_star_approval_event_id=None,
            owner_approval_event_id=None,
        )

    def register_and_approve(route_snapshot: RouteSnapshot, suffix: str) -> None:
        registered = api.post(
            "/api/route-snapshots",
            headers=auth(),
            json={
                "actor_id": OWNER_ID,
                "idempotency_key": f"web:route:{suffix}:register",
                "route_snapshot": route_snapshot.model_dump(mode="json"),
            },
        )
        assert registered.status_code == 201, registered.text
        for approval in ("s3_star", "owner"):
            approved = api.post(
                f"/api/route-snapshots/{route_snapshot.snapshot_id}/approvals",
                headers=auth(),
                json={
                    "actor_id": OWNER_ID,
                    "idempotency_key": (
                        f"web:route:{suffix}:approve:{approval}"
                    ),
                    "approval": approval,
                },
            )
            assert approved.status_code == 200, approved.text

    current = snapshot("route:web-current", candidate.key)
    replacement = snapshot("route:web-replacement", candidate.key)
    register_and_approve(current, "current")
    published = api.post(
        f"/api/route-snapshots/{current.snapshot_id}/publish",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:route:current:publish",
        },
    )
    assert published.status_code == 200, published.text
    register_and_approve(replacement, "replacement")

    duplicate_publish = api.post(
        f"/api/route-snapshots/{replacement.snapshot_id}/publish",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:route:replacement:publish-early",
        },
    )
    assert duplicate_publish.status_code == 409
    invalid_reason = api.post(
        f"/api/route-snapshots/{current.snapshot_id}/retirements",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:route:retire:invalid-reason",
            "reason_code": "free_form_reason",
            "replacement_snapshot_id": replacement.snapshot_id,
        },
    )
    assert invalid_reason.status_code == 422
    missing_replacement = api.post(
        f"/api/route-snapshots/{current.snapshot_id}/retirements",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:route:retire:missing-replacement",
            "reason_code": "superseded_by_approved_snapshot",
        },
    )
    assert missing_replacement.status_code == 422
    decommission_with_replacement = api.post(
        f"/api/route-snapshots/{current.snapshot_id}/retirements",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:route:retire:invalid-decommission",
            "reason_code": "route_decommissioned",
            "replacement_snapshot_id": replacement.snapshot_id,
        },
    )
    assert decommission_with_replacement.status_code == 422

    unknown_candidate = snapshot("route:web-unknown-candidate", "candidate:unknown")
    kernel.register_route_snapshot(
        unknown_candidate,
        actor_id=OWNER_ID,
        idempotency_key="web:route:unknown:register",
    )
    kernel.approve_route_snapshot(
        unknown_candidate.snapshot_id,
        approval="s3_star",
        actor_id=OWNER_ID,
        idempotency_key="web:route:unknown:approve:s3",
    )
    kernel.approve_route_snapshot(
        unknown_candidate.snapshot_id,
        approval="owner",
        actor_id=OWNER_ID,
        idempotency_key="web:route:unknown:approve:owner",
    )
    unregistered = api.post(
        f"/api/route-snapshots/{current.snapshot_id}/retirements",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:route:retire:unknown-candidate",
            "reason_code": "superseded_by_approved_snapshot",
            "replacement_snapshot_id": unknown_candidate.snapshot_id,
        },
    )
    assert unregistered.status_code == 409
    assert "unregistered ModelCandidates" in unregistered.json()["error"]
    del kernel.route_snapshots[unknown_candidate.snapshot_id]

    retired = api.post(
        f"/api/route-snapshots/{current.snapshot_id}/retirements",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:route:retire",
            "reason_code": "superseded_by_approved_snapshot",
            "replacement_snapshot_id": replacement.snapshot_id,
        },
    )
    assert retired.status_code == 201, retired.text
    assert retired.json()["state"] == "retired"
    route_list = api.get("/api/route-snapshots", headers=auth()).json()
    assert route_list["scores"][current.snapshot_id] is None
    assert route_list["routable"][current.snapshot_id] is False
    assert route_list["routable"][replacement.snapshot_id] is False

    decommissioned = snapshot(
        "route:web-decommissioned",
        "candidate:unregistered-decommissioned",
        "interface-and-coding:personal-production",
    )
    kernel.register_route_snapshot(
        decommissioned,
        actor_id=OWNER_ID,
        idempotency_key="web:route:decommissioned:register",
    )
    kernel.approve_route_snapshot(
        decommissioned.snapshot_id,
        approval="s3_star",
        actor_id=OWNER_ID,
        idempotency_key="web:route:decommissioned:approve:s3",
    )
    kernel.approve_route_snapshot(
        decommissioned.snapshot_id,
        approval="owner",
        actor_id=OWNER_ID,
        idempotency_key="web:route:decommissioned:approve:owner",
    )
    kernel.publish_route_snapshot(
        decommissioned.snapshot_id,
        actor_id=OWNER_ID,
        idempotency_key="web:route:decommissioned:publish",
    )
    decommission_response = api.post(
        f"/api/route-snapshots/{decommissioned.snapshot_id}/retirements",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:route:decommissioned:retire",
            "reason_code": "route_decommissioned",
            "replacement_snapshot_id": None,
        },
    )
    assert decommission_response.status_code == 201
    route_list = api.get("/api/route-snapshots", headers=auth()).json()
    assert route_list["scores"][decommissioned.snapshot_id] is None
    assert route_list["routable"][decommissioned.snapshot_id] is False

    replacement_publish = api.post(
        f"/api/route-snapshots/{replacement.snapshot_id}/publish",
        headers=auth(),
        json={
            "actor_id": OWNER_ID,
            "idempotency_key": "web:route:replacement:publish",
        },
    )
    assert replacement_publish.status_code == 200, replacement_publish.text
    route_list = api.get("/api/route-snapshots", headers=auth()).json()
    assert route_list["routable"][replacement.snapshot_id] is True


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
    api = client(system)
    issue = api.post(
        "/api/owner-bootstrap/issues",
        headers=auth(),
        json={
            "base_url": "http://localhost:5173",
            "lifetime_seconds": 60,
            "idempotency_key": "web-bootstrap:issue",
        },
    )
    assert issue.status_code == 201, issue.text
    grant = issue.json()
    assert grant["link"].startswith("http://localhost:5173/owner-bootstrap?code=")
    rejected_origin = api.post(
        "/api/owner-bootstrap/issues",
        headers=auth(),
        json={
            "base_url": "https://untrusted.example",
            "lifetime_seconds": 60,
            "idempotency_key": "web-bootstrap:untrusted-origin",
        },
    )
    assert rejected_origin.status_code == 409
    exchanged = api.post(
        "/api/owner-bootstrap/exchange",
        json={
            "code": grant["code"],
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
            "code": grant["code"],
            "device_id": "owner-browser",
            "idempotency_key": "web-bootstrap:reuse",
        },
    )
    assert reused.status_code == 409
