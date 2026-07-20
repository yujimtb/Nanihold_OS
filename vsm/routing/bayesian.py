from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from vsm.errors import InvariantViolation
from vsm.ids import deterministic_event_id
from vsm.kernel.ledger import OperationalLedger
from vsm.kernel.models import EventEnvelope
from vsm.kernel.models import RouteSnapshot, RouteSnapshotState
from vsm.pilot.models import (
    HandoffPack,
    JudgeKind,
    JudgeObservation,
    ModelCandidate,
)


class BenchmarkPrior(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal[
        "artificial-analysis",
        "terminal-bench",
        "swe-bench",
        "bfcl",
        "local-verification",
    ]
    benchmark_family: Literal["coding", "tool_use", "interface"]
    version: str = Field(min_length=1)
    sample_count: int = Field(gt=0)
    harness: str = Field(min_length=1)
    successes: int = Field(ge=0)
    failures: int = Field(ge=0)
    log_token_samples: tuple[float, ...]
    log_cost_samples: tuple[float, ...]
    log_latency_samples: tuple[float, ...]

    @classmethod
    def _finite_samples(cls, samples: tuple[float, ...], label: str) -> None:
        if not samples or any(not math.isfinite(value) for value in samples):
            raise ValueError(
                f"BenchmarkPrior {label} samples must be non-empty and finite"
            )

    def model_post_init(self, __context) -> None:
        self._finite_samples(self.log_token_samples, "token")
        self._finite_samples(self.log_cost_samples, "cost")
        self._finite_samples(self.log_latency_samples, "latency")


@dataclass
class BetaPosterior:
    alpha: float
    beta: float

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def update(self, success: bool) -> None:
        if success:
            self.alpha += 1.0
        else:
            self.beta += 1.0


@dataclass
class NormalInverseGamma:
    mu: float = 0.0
    lam: float = 1e-6
    alpha: float = 1.0
    beta: float = 1.0

    def update(self, value: float) -> None:
        old_mu = self.mu
        old_lam = self.lam
        self.lam = old_lam + 1.0
        self.mu = (old_lam * old_mu + value) / self.lam
        self.alpha += 0.5
        self.beta += 0.5 * old_lam * (value - old_mu) ** 2 / self.lam

    @property
    def geometric_mean(self) -> float:
        return math.exp(self.mu)


@dataclass
class ConfusionMatrix:
    true_positive: int = 0
    true_negative: int = 0
    false_positive: int = 0
    false_negative: int = 0

    def update(self, predicted: bool, verified: bool) -> None:
        if predicted and verified:
            self.true_positive += 1
        elif not predicted and not verified:
            self.true_negative += 1
        elif predicted:
            self.false_positive += 1
        else:
            self.false_negative += 1


@dataclass
class CandidatePosterior:
    candidate: ModelCandidate
    success: BetaPosterior
    tokens: NormalInverseGamma = field(default_factory=NormalInverseGamma)
    cost: NormalInverseGamma = field(default_factory=NormalInverseGamma)
    latency: NormalInverseGamma = field(default_factory=NormalInverseGamma)
    judge_confusion: ConfusionMatrix = field(default_factory=ConfusionMatrix)
    verified_samples: int = 0
    deterministic_or_human_samples: int = 0
    prior_sources: list[BenchmarkPrior] = field(default_factory=list)


class RouteScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_key: str
    reliability: float
    expected_tokens: float
    expected_cost: float
    expected_latency_ms: float
    expected_utility: float
    ranks: dict[str, int]


class EscalationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_candidate_key: str
    continue_expected_tokens: float
    handoff_expected_tokens: float
    reason: Literal["continue_luna", "handoff_to_sol"]
    handoff_pack: HandoffPack | None


class VerifiedRouteOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome_id: str = Field(pattern=r"^outcome:[A-Za-z0-9._~-]{1,160}$")
    candidate_key: str = Field(min_length=1)
    occurred_at: datetime
    success: bool
    tokens: int = Field(gt=0)
    cost: float = Field(gt=0)
    latency_ms: int = Field(gt=0)
    judge: JudgeObservation | None


class BayesianRouter:
    PUBLIC_CODING_PRIORS = frozenset(
        {"artificial-analysis", "terminal-bench", "swe-bench"}
    )
    PUBLIC_TOOL_PRIORS = frozenset({"bfcl"})
    PRIOR_FAMILIES = {
        "artificial-analysis": "coding",
        "terminal-bench": "coding",
        "swe-bench": "coding",
        "bfcl": "tool_use",
        "local-verification": "interface",
    }

    def __init__(
        self,
        *,
        expected_utility_quality_weight: float,
        expected_utility_cost_weight: float,
        expected_utility_latency_weight: float,
    ) -> None:
        if expected_utility_quality_weight <= 0:
            raise InvariantViolation("quality weight must be positive")
        if expected_utility_cost_weight < 0 or expected_utility_latency_weight < 0:
            raise InvariantViolation("cost and latency weights must be non-negative")
        self.quality_weight = expected_utility_quality_weight
        self.cost_weight = expected_utility_cost_weight
        self.latency_weight = expected_utility_latency_weight
        self._posteriors: dict[str, CandidatePosterior] = {}

    def register(
        self, candidate: ModelCandidate, priors: tuple[BenchmarkPrior, ...]
    ) -> None:
        if candidate.key in self._posteriors:
            raise InvariantViolation("ModelCandidate is already registered")
        if not priors:
            raise InvariantViolation("ModelCandidate requires an explicit prior")
        alpha = 1.0
        beta = 1.0
        posterior = CandidatePosterior(
            candidate=candidate,
            success=BetaPosterior(alpha=alpha, beta=beta),
        )
        for prior in priors:
            expected_family = self.PRIOR_FAMILIES[prior.source]
            if prior.benchmark_family != expected_family:
                raise InvariantViolation(
                    f"benchmark source/family mismatch: {prior.source}"
                )
            if prior.successes + prior.failures != prior.sample_count:
                raise InvariantViolation("benchmark sample_count does not match outcomes")
            alpha += prior.successes
            beta += prior.failures
            for value in prior.log_token_samples:
                posterior.tokens.update(value)
            for value in prior.log_cost_samples:
                posterior.cost.update(value)
            for value in prior.log_latency_samples:
                posterior.latency.update(value)
            posterior.prior_sources.append(prior)
        posterior.success = BetaPosterior(alpha=alpha, beta=beta)
        self._posteriors[candidate.key] = posterior

    def update_verified(
        self,
        *,
        candidate_key: str,
        success: bool,
        tokens: int,
        cost: float,
        latency_ms: int,
        judge: JudgeObservation | None,
    ) -> None:
        posterior = self.validate_verified(
            candidate_key=candidate_key,
            tokens=tokens,
            cost=cost,
            latency_ms=latency_ms,
            judge=judge,
        )
        posterior.success.update(success)
        posterior.tokens.update(math.log(tokens))
        posterior.cost.update(math.log(cost))
        posterior.latency.update(math.log(latency_ms))
        posterior.verified_samples += 1
        if judge is None or judge.kind in (JudgeKind.DETERMINISTIC, JudgeKind.HUMAN):
            posterior.deterministic_or_human_samples += 1
        if judge is not None:
            posterior.judge_confusion.update(
                judge.predicted_success, judge.verified_success
            )

    def validate_verified(
        self,
        *,
        candidate_key: str,
        tokens: int,
        cost: float,
        latency_ms: int,
        judge: JudgeObservation | None,
    ) -> CandidatePosterior:
        posterior = self._require(candidate_key)
        if tokens <= 0 or cost <= 0 or latency_ms <= 0:
            raise InvariantViolation("verified metrics must be positive")
        if judge is not None:
            if judge.candidate_key != candidate_key:
                raise InvariantViolation("JudgeObservation candidate mismatch")
            if judge.verified_success is None:
                raise InvariantViolation("verified router update requires verified Judge outcome")
        return posterior

    def _require(self, candidate_key: str) -> CandidatePosterior:
        try:
            return self._posteriors[candidate_key]
        except KeyError as exc:
            raise InvariantViolation(
                f"unregistered ModelCandidate: {candidate_key}"
            ) from exc

    def scores(self, candidate_keys: tuple[str, ...]) -> list[RouteScore]:
        if not candidate_keys:
            raise InvariantViolation("routing requires at least one candidate")
        raw: dict[str, dict[str, float]] = {}
        for key in candidate_keys:
            posterior = self._require(key)
            reliability = posterior.success.mean
            expected_tokens = posterior.tokens.geometric_mean
            expected_cost = posterior.cost.geometric_mean
            expected_latency = posterior.latency.geometric_mean
            raw[key] = {
                "reliability": reliability,
                "tokens": expected_tokens,
                "cost": expected_cost,
                "latency": expected_latency,
                "utility": self.quality_weight * reliability
                - self.cost_weight * expected_cost
                - self.latency_weight * expected_latency,
            }
        orderings = {
            "reliability_then_cost": sorted(
                candidate_keys,
                key=lambda key: (-raw[key]["reliability"], raw[key]["cost"], key),
            ),
            "expected_utility": sorted(
                candidate_keys, key=lambda key: (-raw[key]["utility"], key)
            ),
            "quality_max": sorted(
                candidate_keys, key=lambda key: (-raw[key]["reliability"], key)
            ),
        }
        return [
            RouteScore(
                candidate_key=key,
                reliability=raw[key]["reliability"],
                expected_tokens=raw[key]["tokens"],
                expected_cost=raw[key]["cost"],
                expected_latency_ms=raw[key]["latency"],
                expected_utility=raw[key]["utility"],
                ranks={
                    objective: ordering.index(key) + 1
                    for objective, ordering in orderings.items()
                },
            )
            for key in candidate_keys
        ]

    def select_production(self, snapshot: RouteSnapshot) -> RouteScore:
        if snapshot.state is not RouteSnapshotState.PUBLISHED:
            raise InvariantViolation("production routing requires a published RouteSnapshot")
        scores = self.scores(snapshot.candidate_keys)
        objective = snapshot.production_objective
        selected = min(scores, key=lambda score: score.ranks[objective])
        posterior = self._require(selected.candidate_key)
        if (
            posterior.verified_samples > 0
            and posterior.deterministic_or_human_samples == 0
        ):
            raise InvariantViolation(
                "AI Judge evidence alone cannot promote a production route"
            )
        return selected

    def escalation_decision(
        self,
        *,
        luna_key: str,
        sol_key: str,
        sol_handoff_tokens: int,
        handoff_pack: HandoffPack,
    ) -> EscalationDecision:
        luna = self._require(luna_key)
        sol = self._require(sol_key)
        if (
            luna.candidate.model_snapshot != "gpt-5.6-luna"
            or luna.candidate.effort != "xhigh"
            or sol.candidate.model_snapshot != "gpt-5.6-sol"
            or sol.candidate.effort != "xhigh"
        ):
            raise InvariantViolation(
                "explicit coding escalation is gpt-5.6-luna/xhigh to gpt-5.6-sol/xhigh"
            )
        if sol_handoff_tokens < 0:
            raise InvariantViolation("handoff token estimate must be non-negative")
        luna_remaining = luna.tokens.geometric_mean / max(luna.success.mean, 1e-9)
        sol_remaining = (
            float(sol_handoff_tokens)
            + sol.tokens.geometric_mean / max(sol.success.mean, 1e-9)
        )
        if sol_remaining < luna_remaining:
            return EscalationDecision(
                selected_candidate_key=sol_key,
                continue_expected_tokens=luna_remaining,
                handoff_expected_tokens=sol_remaining,
                reason="handoff_to_sol",
                handoff_pack=handoff_pack,
            )
        return EscalationDecision(
            selected_candidate_key=luna_key,
            continue_expected_tokens=luna_remaining,
            handoff_expected_tokens=sol_remaining,
            reason="continue_luna",
            handoff_pack=None,
        )


class RoutingEvidenceService:
    """Persists verified outcomes before updating Bayesian posteriors."""

    def __init__(
        self,
        *,
        router: BayesianRouter,
        ledger: OperationalLedger,
        data_space_id: str,
        clock,
    ) -> None:
        self.router = router
        self.ledger = ledger
        self.data_space_id = data_space_id
        self.clock = clock
        self.outcomes: dict[str, VerifiedRouteOutcome] = {}
        self._versions: dict[str, int] = {}
        self.evidence_cursor = 0

    def record(
        self,
        outcome: VerifiedRouteOutcome,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        if outcome.outcome_id in self.outcomes:
            raise InvariantViolation(
                f"verified route outcome already exists: {outcome.outcome_id}"
            )
        self.router.validate_verified(
            candidate_key=outcome.candidate_key,
            tokens=outcome.tokens,
            cost=outcome.cost,
            latency_ms=outcome.latency_ms,
            judge=outcome.judge,
        )
        expected = self._versions.get(outcome.outcome_id, 0)
        event = EventEnvelope(
            event_id=deterministic_event_id(
                data_space_id=self.data_space_id,
                stream_id=outcome.outcome_id,
                idempotency_key=idempotency_key,
            ),
            data_space_id=self.data_space_id,
            stream_id=outcome.outcome_id,
            stream_version=expected + 1,
            event_type="model_outcome_verified",
            occurred_at=self.clock(),
            actor_type="human",
            actor_id=actor_id,
            correlation_id=outcome.outcome_id,
            causation_id=None,
            idempotency_key=idempotency_key,
            payload={
                "outcome": outcome.model_dump(mode="json"),
            },
        )
        result = self.ledger.append(event, expected)
        self._versions[outcome.outcome_id] = result.stream_version
        self.evidence_cursor = max(self.evidence_cursor, result.cursor)
        self._apply(outcome)
        return event

    def replay(self, outcome: VerifiedRouteOutcome, *, stream_version: int, cursor: int) -> None:
        if outcome.outcome_id in self.outcomes:
            raise InvariantViolation(
                f"duplicate verified route outcome: {outcome.outcome_id}"
            )
        self._apply(outcome)
        self._versions[outcome.outcome_id] = stream_version
        self.evidence_cursor = max(self.evidence_cursor, cursor)

    def _apply(self, outcome: VerifiedRouteOutcome) -> None:
        self.router.update_verified(
            candidate_key=outcome.candidate_key,
            success=outcome.success,
            tokens=outcome.tokens,
            cost=outcome.cost,
            latency_ms=outcome.latency_ms,
            judge=outcome.judge,
        )
        self.outcomes[outcome.outcome_id] = outcome
