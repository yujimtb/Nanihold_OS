from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from vsm.errors import InvariantViolation
from vsm.ids import deterministic_event_id
from vsm.kernel.ledger import OperationalLedger
from vsm.kernel.models import EventEnvelope


class TokenIncidentKind(StrEnum):
    CALL_DENSITY = "call_density"
    CONTEXT_RELOAD = "context_reload"
    MODEL_CALL_POLLING = "model_call_polling"
    PERMISSION_CLASSIFIER = "permission_classifier"
    MODEL_SUBSTITUTION = "model_substitution"
    REEDIT = "reedit"
    FALSE_COMPLETE = "false_complete"


class TokenObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(min_length=1)
    work_type: str = Field(min_length=1)
    occurred_at: datetime
    total_input_tokens: int = Field(ge=0)
    interface_input_tokens: int = Field(ge=0)
    incident_kinds: frozenset[TokenIncidentKind]
    full_history_resent: bool
    expensive_interface_calls: int = Field(ge=0)
    verified_complete: bool


class TokenBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_type: str = Field(min_length=1)
    approved_mean_input_tokens: float = Field(gt=0)
    approved_mean_interface_tokens: float = Field(gt=0)
    approved_at: datetime


class InvestigationTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    work_type: str
    observation_ids: tuple[str, ...]
    logic_only: bool
    allowed_model: str | None
    allowed_effort: str | None


class AcceptanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_input_reduction: float
    interface_input_reduction: float
    ux_golden_passed: int
    ux_golden_total: int
    model_call_polling: int
    sandbox_classifier_triggers: int
    full_history_resends: int
    false_complete: int
    accepted: bool


class TokenEfficiencyLab:
    SINGLE_EVENT_TRIGGERS = frozenset(
        {
            TokenIncidentKind.PERMISSION_CLASSIFIER,
            TokenIncidentKind.MODEL_SUBSTITUTION,
            TokenIncidentKind.CONTEXT_RELOAD,
            TokenIncidentKind.MODEL_CALL_POLLING,
            TokenIncidentKind.FALSE_COMPLETE,
        }
    )

    def __init__(self) -> None:
        self.baselines: dict[str, TokenBaseline] = {}
        self.observations: list[TokenObservation] = []
        self.last_weekly_review_at: datetime | None = None

    def approve_baseline(self, baseline: TokenBaseline) -> None:
        self.baselines[baseline.work_type] = baseline

    def evaluate(self, observation: TokenObservation) -> list[InvestigationTrigger]:
        triggers: list[InvestigationTrigger] = []
        direct = observation.incident_kinds & self.SINGLE_EVENT_TRIGGERS
        if observation.full_history_resent:
            direct = direct | {TokenIncidentKind.CONTEXT_RELOAD}
        for incident in sorted(direct):
            triggers.append(
                InvestigationTrigger(
                    reason=f"single_event:{incident}",
                    work_type=observation.work_type,
                    observation_ids=(observation.observation_id,),
                    logic_only=True,
                    allowed_model=None,
                    allowed_effort=None,
                )
            )
        comparable = [
            item
            for item in (*self.observations, observation)
            if item.work_type == observation.work_type
        ]
        baseline = self.baselines.get(observation.work_type)
        if len(comparable) >= 20 and baseline is not None:
            mean = sum(item.total_input_tokens for item in comparable) / len(comparable)
            if mean >= baseline.approved_mean_input_tokens * 1.10:
                triggers.append(
                    InvestigationTrigger(
                        reason="twenty_item_mean_degraded_by_ten_percent",
                        work_type=observation.work_type,
                        observation_ids=tuple(
                            item.observation_id for item in comparable[-20:]
                        ),
                        logic_only=True,
                        allowed_model=None,
                        allowed_effort=None,
                    )
                )
        return triggers

    def observe(self, observation: TokenObservation) -> list[InvestigationTrigger]:
        if any(
            item.observation_id == observation.observation_id
            for item in self.observations
        ):
            raise InvariantViolation(
                f"TokenObservation already exists: {observation.observation_id}"
            )
        triggers = self.evaluate(observation)
        self.observations.append(observation)
        return triggers

    def authorize_model_experiment(
        self, trigger: InvestigationTrigger, *, model: str, effort: str
    ) -> InvestigationTrigger:
        lowered = model.lower()
        if "opus" in lowered:
            raise InvariantViolation("Token Efficiency Lab forbids Opus")
        if effort != "low":
            raise InvariantViolation("Token Efficiency Lab model effort must be low")
        return trigger.model_copy(
            update={
                "logic_only": False,
                "allowed_model": model,
                "allowed_effort": effort,
            }
        )

    def weekly_due(self, now: datetime) -> bool:
        if not self.observations:
            return False
        reference = self.last_weekly_review_at or min(
            item.occurred_at for item in self.observations
        )
        return now - reference >= timedelta(days=7)

    def mark_weekly_reviewed(self, reviewed_at: datetime) -> None:
        if (
            self.last_weekly_review_at is not None
            and reviewed_at <= self.last_weekly_review_at
        ):
            raise InvariantViolation("Token Lab weekly review time must advance")
        self.last_weekly_review_at = reviewed_at

    def acceptance_report(
        self,
        *,
        before_total_input: int,
        after_total_input: int,
        before_interface_input: int,
        after_interface_input: int,
        ux_golden_passed: int,
        ux_golden_total: int,
        sandbox_classifier_triggers: int,
    ) -> AcceptanceReport:
        if before_total_input <= 0 or before_interface_input <= 0:
            raise InvariantViolation("before token totals must be positive")
        total_reduction = 1.0 - after_total_input / before_total_input
        interface_reduction = 1.0 - after_interface_input / before_interface_input
        incident_counts = Counter(
            kind for item in self.observations for kind in item.incident_kinds
        )
        full_history = sum(item.full_history_resent for item in self.observations)
        false_complete = incident_counts[TokenIncidentKind.FALSE_COMPLETE]
        polling = incident_counts[TokenIncidentKind.MODEL_CALL_POLLING]
        accepted = all(
            (
                total_reduction >= 0.50,
                interface_reduction >= 0.70,
                ux_golden_passed == ux_golden_total,
                polling == 0,
                sandbox_classifier_triggers == 0,
                full_history == 0,
                false_complete == 0,
            )
        )
        return AcceptanceReport(
            total_input_reduction=total_reduction,
            interface_input_reduction=interface_reduction,
            ux_golden_passed=ux_golden_passed,
            ux_golden_total=ux_golden_total,
            model_call_polling=polling,
            sandbox_classifier_triggers=sandbox_classifier_triggers,
            full_history_resends=full_history,
            false_complete=false_complete,
            accepted=accepted,
        )


class TokenLabEventService:
    """Persists baselines and observations before applying deterministic lab logic."""

    def __init__(
        self,
        *,
        lab: TokenEfficiencyLab,
        ledger: OperationalLedger,
        data_space_id: str,
        clock: Callable[[], datetime],
    ) -> None:
        self.lab = lab
        self.ledger = ledger
        self.data_space_id = data_space_id
        self.clock = clock
        self._versions: dict[str, int] = {}

    @staticmethod
    def _baseline_stream(work_type: str) -> str:
        import hashlib

        digest = hashlib.sha256(work_type.encode("utf-8")).hexdigest()
        return f"tokenlab:{digest}"

    def approve_baseline(
        self,
        baseline: TokenBaseline,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        stream_id = self._baseline_stream(baseline.work_type)
        expected = self._versions.get(stream_id, 0)
        event = EventEnvelope(
            event_id=deterministic_event_id(
                data_space_id=self.data_space_id,
                stream_id=stream_id,
                idempotency_key=idempotency_key,
            ),
            data_space_id=self.data_space_id,
            stream_id=stream_id,
            stream_version=expected + 1,
            event_type="token_baseline_approved",
            occurred_at=self.clock(),
            actor_type="human",
            actor_id=actor_id,
            correlation_id=stream_id,
            causation_id=None,
            idempotency_key=idempotency_key,
            payload={"baseline": baseline.model_dump(mode="json")},
        )
        result = self.ledger.append(event, expected)
        self._versions[stream_id] = result.stream_version
        self.lab.approve_baseline(baseline)
        return event

    def observe(
        self,
        observation: TokenObservation,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> tuple[EventEnvelope, list[InvestigationTrigger]]:
        if any(
            item.observation_id == observation.observation_id
            for item in self.lab.observations
        ):
            raise InvariantViolation(
                f"TokenObservation already exists: {observation.observation_id}"
            )
        triggers = self.lab.evaluate(observation)
        stream_id = observation.observation_id
        expected = self._versions.get(stream_id, 0)
        event = EventEnvelope(
            event_id=deterministic_event_id(
                data_space_id=self.data_space_id,
                stream_id=stream_id,
                idempotency_key=idempotency_key,
            ),
            data_space_id=self.data_space_id,
            stream_id=stream_id,
            stream_version=expected + 1,
            event_type="token_observation_recorded",
            occurred_at=self.clock(),
            actor_type="system",
            actor_id=actor_id,
            correlation_id=stream_id,
            causation_id=None,
            idempotency_key=idempotency_key,
            payload={
                "observation": observation.model_dump(mode="json"),
                "triggers": [
                    trigger.model_dump(mode="json") for trigger in triggers
                ],
            },
        )
        result = self.ledger.append(event, expected)
        self._versions[stream_id] = result.stream_version
        self.lab.observe(observation)
        return event, triggers

    def replay_baseline(
        self, baseline: TokenBaseline, *, stream_id: str, stream_version: int
    ) -> None:
        self.lab.approve_baseline(baseline)
        self._versions[stream_id] = stream_version

    def replay_observation(
        self, observation: TokenObservation, *, stream_id: str, stream_version: int
    ) -> None:
        self.lab.observe(observation)
        self._versions[stream_id] = stream_version

    def record_weekly_review(
        self,
        reviewed_at: datetime,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> EventEnvelope:
        if not self.lab.weekly_due(reviewed_at):
            raise InvariantViolation("Token Lab weekly review is not due")
        stream_id = "tokenlab:weekly-review"
        expected = self._versions.get(stream_id, 0)
        event = EventEnvelope(
            event_id=deterministic_event_id(
                data_space_id=self.data_space_id,
                stream_id=stream_id,
                idempotency_key=idempotency_key,
            ),
            data_space_id=self.data_space_id,
            stream_id=stream_id,
            stream_version=expected + 1,
            event_type="token_weekly_review_recorded",
            occurred_at=self.clock(),
            actor_type="system",
            actor_id=actor_id,
            correlation_id=stream_id,
            causation_id=None,
            idempotency_key=idempotency_key,
            payload={"reviewed_at": reviewed_at.isoformat()},
        )
        result = self.ledger.append(event, expected)
        self._versions[stream_id] = result.stream_version
        self.lab.mark_weekly_reviewed(reviewed_at)
        return event

    def replay_weekly_review(
        self, reviewed_at: datetime, *, stream_id: str, stream_version: int
    ) -> None:
        self.lab.mark_weekly_reviewed(reviewed_at)
        self._versions[stream_id] = stream_version
