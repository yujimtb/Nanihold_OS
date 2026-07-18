"""Economics Event の versioned registry。

既存 Event_Log の legacy event type 列へ直接継ぎ足さず、生存計測側で
独立した schema version を管理する。
"""

from __future__ import annotations

from typing import Any, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


ECONOMICS_SCHEMA_VERSION = 1
ECONOMICS_EVENT_TYPES = (
    "finance_entry_recorded",
    "recurring_commitment_recorded",
    "usage_recorded",
    "price_profile_activated",
    "fx_rate_recorded",
    "run_cost_finalized",
    "invoice_reconciled",
    "survival_report_generated",
    "economic_measurement_failed",
    "economic_pain_detected",
)


class _EconomicsPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


class FinanceEntryPayload(_EconomicsPayload):
    entry_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    signed_amount_jpy: int


class UsageRecordedPayload(_EconomicsPayload):
    usage_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    cost_status: str = Field(min_length=1)


class PriceProfileActivatedPayload(_EconomicsPayload):
    profile_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)


class FxRateRecordedPayload(_EconomicsPayload):
    rate_id: str = Field(min_length=1)
    base_currency: str = Field(min_length=3, max_length=3)
    quote_currency: str = Field(min_length=3, max_length=3)


class RunCostFinalizedPayload(_EconomicsPayload):
    run_id: str = Field(min_length=1)
    cost_status: str = Field(min_length=1)


class SurvivalReportGeneratedPayload(_EconomicsPayload):
    report_date: str = Field(min_length=10)
    input_hash: str = Field(min_length=1)


class EconomicMeasurementFailedPayload(_EconomicsPayload):
    subject_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


ECONOMICS_PAYLOAD_MODELS: Mapping[str, type[BaseModel]] = {
    "finance_entry_recorded": FinanceEntryPayload,
    "recurring_commitment_recorded": _EconomicsPayload,
    "usage_recorded": UsageRecordedPayload,
    "price_profile_activated": PriceProfileActivatedPayload,
    "fx_rate_recorded": FxRateRecordedPayload,
    "run_cost_finalized": RunCostFinalizedPayload,
    "invoice_reconciled": _EconomicsPayload,
    "survival_report_generated": SurvivalReportGeneratedPayload,
    "economic_measurement_failed": EconomicMeasurementFailedPayload,
    "economic_pain_detected": EconomicMeasurementFailedPayload,
}


class EconomicEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    schema_version: int = ECONOMICS_SCHEMA_VERSION
    event_type: str = Field(min_length=1)
    payload: dict[str, Any]


def validate_economic_event(event_type: str, payload: dict[str, Any]) -> EconomicEvent:
    if event_type not in ECONOMICS_PAYLOAD_MODELS:
        raise ValueError(f"unknown economics event type: {event_type}")
    model = ECONOMICS_PAYLOAD_MODELS[event_type]
    validated = model.model_validate(payload)
    return EconomicEvent(
        event_type=event_type,
        payload=validated.model_dump(mode="json"),
    )
