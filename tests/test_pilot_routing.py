from __future__ import annotations

import math
from datetime import timedelta

import pytest

from conftest import NOW
from vsm.errors import InvariantViolation, ModelMismatch
from vsm.kernel.models import RouteSnapshot, RouteSnapshotState
from vsm.pilot.claude import ClaudePilotAdapter
from vsm.pilot.models import (
    HandoffPack,
    JudgeKind,
    JudgeObservation,
    ModelCandidate,
    PilotMode,
    PilotPolicy,
    PilotRequest,
    PilotResponse,
    SandboxProfile,
)
from vsm.routing.bayesian import (
    BayesianRouter,
    BenchmarkPrior,
    RoutingEvidenceService,
    VerifiedRouteOutcome,
)


def candidate(
    model: str,
    effort: str,
    *,
    environment: str = "linux-amd64:2026-07",
) -> ModelCandidate:
    return ModelCandidate(
        adapter="codex" if model.startswith("gpt") else "claude-code",
        adapter_version="1.0.0",
        provider="openai" if model.startswith("gpt") else "anthropic",
        selection="exact",
        model_snapshot=model,
        effort=effort,
        toolset=("filesystem", "git"),
        sandbox_fingerprint="sandbox:v1",
        environment_fingerprint=environment,
    )


def prior(successes: int, failures: int, source: str = "swe-bench") -> BenchmarkPrior:
    return BenchmarkPrior(
        source=source,
        benchmark_family="tool_use" if source == "bfcl" else "coding",
        version="2026-07",
        sample_count=successes + failures,
        harness="verified-fixed",
        successes=successes,
        failures=failures,
        log_token_samples=(math.log(1000), math.log(1200)),
        log_cost_samples=(math.log(0.1), math.log(0.12)),
        log_latency_samples=(math.log(100), math.log(120)),
    )


def router() -> BayesianRouter:
    return BayesianRouter(
        expected_utility_quality_weight=10,
        expected_utility_cost_weight=1,
        expected_utility_latency_weight=0.001,
    )


def test_candidate_key_separates_version_and_environment():
    base = candidate("gpt-5.6-luna", "xhigh")
    changed = candidate(
        "gpt-5.6-luna", "xhigh", environment="windows-amd64:2026-07"
    )
    assert base.key != changed.key


def test_three_objectives_and_ai_judge_cannot_promote_alone():
    route = router()
    first = candidate("gpt-5.6-luna", "xhigh")
    second = candidate("gpt-5.6-sol", "xhigh")
    route.register(first, (prior(8, 2),))
    route.register(second, (prior(9, 1),))
    judge = JudgeObservation(
        candidate_key=second.key,
        kind=JudgeKind.CHEAP_AI,
        predicted_success=True,
        verified_success=True,
        judge_model="gpt-5.6-luna",
        judge_effort="low",
    )
    route.update_verified(
        candidate_key=second.key,
        success=True,
        tokens=800,
        cost=0.2,
        latency_ms=80,
        judge=judge,
    )
    scores = route.scores((first.key, second.key))
    assert all(
        set(score.ranks)
        == {"reliability_then_cost", "expected_utility", "quality_max"}
        for score in scores
    )
    snapshot = RouteSnapshot(
        snapshot_id="route:prod",
        data_space_id="space:personal",
        route_key="coding_s1",
        evidence_cursor=10,
        candidate_keys=(first.key, second.key),
        production_objective="quality_max",
        state=RouteSnapshotState.PUBLISHED,
        s3_star_approval_event_id="event:s3",
        owner_approval_event_id="event:owner",
    )
    with pytest.raises(InvariantViolation):
        route.select_production(snapshot)
    route.update_verified(
        candidate_key=second.key,
        success=True,
        tokens=700,
        cost=0.2,
        latency_ms=70,
        judge=JudgeObservation(
            candidate_key=second.key,
            kind=JudgeKind.DETERMINISTIC,
            predicted_success=True,
            verified_success=True,
            judge_model=None,
            judge_effort=None,
        ),
    )
    assert route.select_production(snapshot).candidate_key == second.key


def test_human_approved_route_can_bootstrap_from_public_prior_only():
    route = router()
    candidate_with_prior = candidate("gpt-5.6-sol", "xhigh")
    route.register(candidate_with_prior, (prior(9, 1),))
    snapshot = RouteSnapshot(
        snapshot_id="route:prior-only",
        data_space_id="space:personal",
        route_key="coding_s1",
        evidence_cursor=0,
        candidate_keys=(candidate_with_prior.key,),
        production_objective="quality_max",
        state=RouteSnapshotState.PUBLISHED,
        s3_star_approval_event_id="event:s3",
        owner_approval_event_id="event:owner",
    )

    assert (
        route.select_production(snapshot).candidate_key == candidate_with_prior.key
    )


def test_luna_to_sol_has_no_retry_count_and_recomputes_expected_remaining_tokens():
    route = router()
    luna = candidate("gpt-5.6-luna", "xhigh")
    sol = candidate("gpt-5.6-sol", "xhigh")
    route.register(luna, (prior(1, 9),))
    route.register(sol, (prior(9, 1),))
    pack = HandoffPack(
        work_item_id="work:coding",
        unmet_acceptance=("test still fails",),
        gate_differences=("one deterministic gate",),
        artifact_refs=("artifact:patch",),
        decision_refs=("decision:architecture",),
    )
    decision = route.escalation_decision(
        luna_key=luna.key,
        sol_key=sol.key,
        sol_handoff_tokens=100,
        handoff_pack=pack,
    )
    assert decision.reason == "handoff_to_sol"
    assert decision.handoff_pack == pack
    route.update_verified(
        candidate_key=luna.key,
        success=True,
        tokens=100,
        cost=0.01,
        latency_ms=10,
        judge=None,
    )
    updated = route.escalation_decision(
        luna_key=luna.key,
        sol_key=sol.key,
        sol_handoff_tokens=100,
        handoff_pack=pack,
    )
    assert updated.continue_expected_tokens != decision.continue_expected_tokens


def test_verified_routing_evidence_is_persisted_before_posterior_update(system):
    kernel, ledger, _, _ = system
    route = router()
    model = candidate("gpt-5.6-luna", "xhigh")
    route.register(model, (prior(8, 2),))
    evidence = RoutingEvidenceService(
        router=route,
        ledger=ledger,
        data_space_id=kernel.data_space.data_space_id,
        clock=kernel.clock,
    )
    outcome = VerifiedRouteOutcome(
        outcome_id="outcome:verified-one",
        candidate_key=model.key,
        occurred_at=NOW,
        success=True,
        tokens=1000,
        cost=0.1,
        latency_ms=100,
        judge=JudgeObservation(
            candidate_key=model.key,
            kind=JudgeKind.DETERMINISTIC,
            predicted_success=True,
            verified_success=True,
            judge_model=None,
            judge_effort=None,
        ),
    )
    evidence.record(
        outcome,
        actor_id="owner:primary",
        idempotency_key="routing:evidence-one",
    )
    assert evidence.evidence_cursor > 0
    assert evidence.outcomes[outcome.outcome_id] == outcome
    assert ledger.page(evidence.evidence_cursor - 1, 1)[0].event.event_type == (
        "model_outcome_verified"
    )


def test_claude_permission_modes_are_isolated_and_model_mismatch_stops():
    profile = SandboxProfile(
        profile_id="sandbox:profile",
        certificate_sha256="a" * 64,
        filesystem_write_roots=("/workspace",),
        network_destinations=("github.com",),
        issued_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    policy = PilotPolicy(
        mode=PilotMode.SANDBOXED_BYPASS,
        sandbox_profile=profile,
        permission_classifier_enabled=False,
        writes_allowed=True,
    )
    model = candidate("claude-haiku-4-5-20251001", "high")
    adapter = ClaudePilotAdapter(adapter_version="1.0.0", policy=policy)
    request = PilotRequest(
        execution_id="execution:test",
        work_item_id="work:test",
        requested_candidate=model,
        prompt="perform bounded task",
        provider_session_id="session:one",
        effect_capabilities=frozenset({"filesystem_write"}),
    )
    launch = adapter.build_launch(request)
    assert "--dangerously-skip-permissions" in launch.argv
    assert "--resume" in launch.argv
    mismatch = PilotResponse(
        execution_id=request.execution_id,
        requested_candidate_key=model.key,
        actual_provider="anthropic",
        actual_model_snapshot="claude-opus-4-8",
        provider_session_id="session:one",
        text="result",
        input_tokens=1,
        output_tokens=1,
        latency_ms=1,
        classifier_triggered=False,
        permission_rejections=0,
        reedited_tokens=0,
    )
    with pytest.raises(ModelMismatch):
        adapter.validate_response(request, mismatch)
    with pytest.raises(ValueError):
        PilotPolicy(
            mode=PilotMode.MANAGED_PERMISSIONS,
            sandbox_profile=None,
            permission_classifier_enabled=False,
            writes_allowed=True,
        )
