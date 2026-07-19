import math

from conftest import NOW, OWNER_ID, SPACE_ID
from vsm.interface.service import InterfaceService
from vsm.kernel.service import Kernel
from vsm.pilot.models import JudgeKind, JudgeObservation, ModelCandidate
from vsm.projection import OperationalProjection
from vsm.routing.bayesian import (
    BayesianRouter,
    BenchmarkPrior,
    RoutingEvidenceService,
    VerifiedRouteOutcome,
)
from vsm.token_lab.lab import (
    TokenBaseline,
    TokenEfficiencyLab,
    TokenLabEventService,
    TokenObservation,
)


def make_router(candidate: ModelCandidate) -> BayesianRouter:
    router = BayesianRouter(
        expected_utility_quality_weight=1,
        expected_utility_cost_weight=0,
        expected_utility_latency_weight=0,
    )
    router.register(
        candidate,
        (
            BenchmarkPrior(
                source="swe-bench",
                benchmark_family="coding",
                version="verified-500",
                sample_count=2,
                harness="fixed-container",
                successes=1,
                failures=1,
                log_token_samples=(math.log(1000),),
                log_cost_samples=(math.log(0.1),),
                log_latency_samples=(math.log(100),),
            ),
        ),
    )
    return router


def test_projection_rebuilds_routing_and_token_lab_services(system):
    kernel, ledger, interface, pilot = system
    candidate = ModelCandidate(
        adapter="codex",
        adapter_version="1.0.0",
        provider="openai",
        model_snapshot="gpt-5.6-luna",
        effort="xhigh",
        toolset=("filesystem", "git"),
        sandbox_fingerprint="sandbox:test",
        environment_fingerprint="environment:test",
    )
    router = make_router(candidate)
    routing = RoutingEvidenceService(
        router=router,
        ledger=ledger,
        data_space_id=SPACE_ID,
        clock=kernel.clock,
    )
    outcome = VerifiedRouteOutcome(
        outcome_id="outcome:projection",
        candidate_key=candidate.key,
        occurred_at=NOW,
        success=True,
        tokens=900,
        cost=0.09,
        latency_ms=90,
        judge=JudgeObservation(
            candidate_key=candidate.key,
            kind=JudgeKind.HUMAN,
            predicted_success=True,
            verified_success=True,
            judge_model=None,
            judge_effort=None,
        ),
    )
    routing.record(
        outcome,
        actor_id=OWNER_ID,
        idempotency_key="projection:routing",
    )
    lab = TokenEfficiencyLab()
    lab_events = TokenLabEventService(
        lab=lab,
        ledger=ledger,
        data_space_id=SPACE_ID,
        clock=kernel.clock,
    )
    lab_events.approve_baseline(
        TokenBaseline(
            work_type="coding",
            approved_mean_input_tokens=1000,
            approved_mean_interface_tokens=300,
            approved_at=NOW,
        ),
        actor_id=OWNER_ID,
        idempotency_key="projection:baseline",
    )
    lab_events.observe(
        TokenObservation(
            observation_id="observation:projection",
            work_type="coding",
            occurred_at=NOW,
            total_input_tokens=800,
            interface_input_tokens=200,
            incident_kinds=frozenset(),
            full_history_resent=False,
            expensive_interface_calls=1,
            verified_complete=True,
        ),
        actor_id="system:token-lab",
        idempotency_key="projection:observation",
    )

    rebuilt_kernel = Kernel(
        data_space=kernel.data_space,
        ledger=ledger,
        audit_policy=kernel.audit_policy,
        control_policy=kernel.control_policy,
        clock=kernel.clock,
    )
    rebuilt_router = make_router(candidate)
    rebuilt_routing = RoutingEvidenceService(
        router=rebuilt_router,
        ledger=ledger,
        data_space_id=SPACE_ID,
        clock=kernel.clock,
    )
    rebuilt_lab = TokenEfficiencyLab()
    rebuilt_lab_events = TokenLabEventService(
        lab=rebuilt_lab,
        ledger=ledger,
        data_space_id=SPACE_ID,
        clock=kernel.clock,
    )
    rebuilt_interface = InterfaceService(
        kernel=rebuilt_kernel,
        ledger=ledger,
        pilot=pilot,
        token_lab_events=rebuilt_lab_events,
        clock=kernel.clock,
    )
    projection = OperationalProjection(
        kernel=rebuilt_kernel,
        interface=rebuilt_interface,
        routing_evidence=rebuilt_routing,
        token_lab_events=rebuilt_lab_events,
    )
    projection.rebuild(page_size=2)

    assert rebuilt_routing.outcomes == {outcome.outcome_id: outcome}
    assert rebuilt_router._posteriors[candidate.key].verified_samples == 1
    assert rebuilt_lab.baselines["coding"].approved_mean_input_tokens == 1000
    assert rebuilt_lab.observations[0].observation_id == "observation:projection"
