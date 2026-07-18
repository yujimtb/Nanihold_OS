"""経済計測で共有する不変データモデル。

金額は円を ``int``、外貨・単価・為替は ``Decimal`` の文字列表現で
永続化する。浮動小数点をこのドメインへ持ち込まない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} は timezone-aware でなければなりません")
    return value.astimezone(timezone.utc)


def _decimal(value: Decimal | str | int) -> Decimal:
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Decimal は有限値でなければなりません")
    return result


def _iso(value: datetime) -> str:
    return _require_aware(value, "datetime").isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _require_aware(parsed, "datetime")


class LedgerEntryKind(StrEnum):
    EXPENSE = "expense"
    REVENUE = "revenue"
    OWNER_CONTRIBUTION = "owner_contribution"
    CASH_ADJUSTMENT = "cash_adjustment"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Operational cash ledger の一行。

    ``signed_amount_jpy`` は入金を正、出金を負とする。訂正は既存行の
    更新ではなく、``reverses_entry_id`` を設定した反対仕訳で行う。
    """

    entry_id: str
    occurred_at: datetime
    booked_at: datetime
    kind: LedgerEntryKind
    category: str
    signed_amount_jpy: int
    tax_jpy: int
    original_amount: Decimal
    currency: str
    fx_rate_id: str | None
    source: str
    source_id: str
    idempotency_key: str
    run_id: str | None = None
    proposal_id: str | None = None
    opportunity_id: str | None = None
    evidence_ref: str | None = None
    evidence_hash: str | None = None
    reverses_entry_id: str | None = None
    actor_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.entry_id.strip():
            raise ValueError("entry_id は空にできません")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key は空にできません")
        if not self.category.strip() or not self.source.strip() or not self.source_id.strip():
            raise ValueError("category/source/source_id は空にできません")
        if not isinstance(self.signed_amount_jpy, int) or isinstance(self.signed_amount_jpy, bool):
            raise ValueError("signed_amount_jpy は整数でなければなりません")
        if self.kind is LedgerEntryKind.EXPENSE and self.signed_amount_jpy >= 0:
            raise ValueError("expense の signed_amount_jpy は負でなければなりません")
        if self.kind in {LedgerEntryKind.REVENUE, LedgerEntryKind.OWNER_CONTRIBUTION} and self.signed_amount_jpy <= 0:
            raise ValueError("revenue/owner_contribution の signed_amount_jpy は正でなければなりません")
        if not isinstance(self.tax_jpy, int) or isinstance(self.tax_jpy, bool) or self.tax_jpy < 0:
            raise ValueError("tax_jpy は0以上の整数でなければなりません")
        if not self.currency.strip() or len(self.currency.strip()) != 3:
            raise ValueError("currency は3文字でなければなりません")
        if self.currency.upper() == "JPY" and self.fx_rate_id is not None:
            raise ValueError("JPY entry に fx_rate_id は指定できません")
        _require_aware(self.occurred_at, "occurred_at")
        _require_aware(self.booked_at, "booked_at")
        _decimal(self.original_amount)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "occurred_at": _iso(self.occurred_at),
            "booked_at": _iso(self.booked_at),
            "kind": self.kind.value,
            "category": self.category,
            "signed_amount_jpy": self.signed_amount_jpy,
            "tax_jpy": self.tax_jpy,
            "original_amount": str(_decimal(self.original_amount)),
            "currency": self.currency.upper(),
            "fx_rate_id": self.fx_rate_id,
            "source": self.source,
            "source_id": self.source_id,
            "idempotency_key": self.idempotency_key,
            "run_id": self.run_id,
            "proposal_id": self.proposal_id,
            "opportunity_id": self.opportunity_id,
            "evidence_ref": self.evidence_ref,
            "evidence_hash": self.evidence_hash,
            "reverses_entry_id": self.reverses_entry_id,
            "actor_id": self.actor_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LedgerEntry":
        return cls(
            entry_id=str(value["entry_id"]),
            occurred_at=_parse_datetime(str(value["occurred_at"])),
            booked_at=_parse_datetime(str(value["booked_at"])),
            kind=LedgerEntryKind(str(value["kind"])),
            category=str(value["category"]),
            signed_amount_jpy=int(value["signed_amount_jpy"]),
            tax_jpy=int(value["tax_jpy"]),
            original_amount=_decimal(value["original_amount"]),
            currency=str(value["currency"]),
            fx_rate_id=value.get("fx_rate_id"),
            source=str(value["source"]),
            source_id=str(value["source_id"]),
            idempotency_key=str(value["idempotency_key"]),
            run_id=value.get("run_id"),
            proposal_id=value.get("proposal_id"),
            opportunity_id=value.get("opportunity_id"),
            evidence_ref=value.get("evidence_ref"),
            evidence_hash=value.get("evidence_hash"),
            reverses_entry_id=value.get("reverses_entry_id"),
            actor_id=value.get("actor_id"),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """一回の AgentRuntime 呼び出しで観測した利用量。"""

    usage_id: str
    invocation_id: str
    run_id: str
    node_id: str
    provider: str
    backend: str
    model: str
    billing_mode: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    tool_units: Decimal
    started_at: datetime
    ended_at: datetime
    wall_clock_ms: int = 0
    price_profile_id: str | None = None
    event_id: str | None = None
    cost_status: str = "unpriced"
    costing_error: str | None = None
    cost_jpy: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "usage_id",
            "invocation_id",
            "run_id",
            "node_id",
            "provider",
            "backend",
            "model",
            "billing_mode",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} は空にできません")
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} は0以上の整数でなければなりません")
        if _decimal(self.tool_units) < 0:
            raise ValueError("tool_units は0以上でなければなりません")
        _require_aware(self.started_at, "started_at")
        _require_aware(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at は started_at 以後でなければなりません")
        if not isinstance(self.wall_clock_ms, int) or isinstance(self.wall_clock_ms, bool) or self.wall_clock_ms < 0:
            raise ValueError("wall_clock_ms は0以上の整数でなければなりません")
        if self.cost_status not in {"priced", "unpriced", "failed"}:
            raise ValueError("cost_status は priced/unpriced/failed のいずれかです")
        if self.cost_status == "priced" and self.cost_jpy is None:
            raise ValueError("priced usage には cost_jpy が必要です")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "usage_id": self.usage_id,
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "provider": self.provider,
            "backend": self.backend,
            "model": self.model,
            "billing_mode": self.billing_mode,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "tool_units": str(_decimal(self.tool_units)),
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "wall_clock_ms": self.wall_clock_ms,
            "price_profile_id": self.price_profile_id,
            "event_id": self.event_id,
            "cost_status": self.cost_status,
            "costing_error": self.costing_error,
            "cost_jpy": self.cost_jpy,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "UsageRecord":
        return cls(
            usage_id=str(value["usage_id"]),
            invocation_id=str(value["invocation_id"]),
            run_id=str(value["run_id"]),
            node_id=str(value["node_id"]),
            provider=str(value["provider"]),
            backend=str(value["backend"]),
            model=str(value["model"]),
            billing_mode=str(value["billing_mode"]),
            input_tokens=int(value["input_tokens"]),
            output_tokens=int(value["output_tokens"]),
            cache_read_tokens=int(value["cache_read_tokens"]),
            cache_write_tokens=int(value["cache_write_tokens"]),
            tool_units=_decimal(value["tool_units"]),
            started_at=_parse_datetime(str(value["started_at"])),
            ended_at=_parse_datetime(str(value["ended_at"])),
            wall_clock_ms=int(value.get("wall_clock_ms", 0)),
            price_profile_id=value.get("price_profile_id"),
            event_id=value.get("event_id"),
            cost_status=str(value.get("cost_status", "unpriced")),
            costing_error=value.get("costing_error"),
            cost_jpy=value.get("cost_jpy"),
        )


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    direct_jpy: int
    allocated_fixed_jpy: int = 0
    human_shadow_jpy: int = 0
    fully_loaded_jpy: int | None = None
    api_equivalent_shadow_jpy: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "direct_jpy",
            "allocated_fixed_jpy",
            "human_shadow_jpy",
            "api_equivalent_shadow_jpy",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} は0以上の整数でなければなりません")
        if self.fully_loaded_jpy is not None and self.fully_loaded_jpy < 0:
            raise ValueError("fully_loaded_jpy は0以上でなければなりません")

    def to_dict(self) -> dict[str, int | None]:
        return {
            "cash_direct_jpy": self.direct_jpy,
            "allocated_fixed_jpy": self.allocated_fixed_jpy,
            "human_shadow_jpy": self.human_shadow_jpy,
            "fully_loaded_jpy": self.fully_loaded_jpy,
            "api_equivalent_shadow_jpy": self.api_equivalent_shadow_jpy,
        }


@dataclass(frozen=True, slots=True)
class RunCost:
    """Run 単位の原価 projection。"""

    run_id: str
    api_direct_jpy: int
    allocated_fixed_jpy: int
    human_shadow_jpy: int
    electricity_jpy: int
    cac_jpy: int
    risk_reserve_jpy: int
    status: str
    usage_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id は空にできません")
        for name in (
            "api_direct_jpy",
            "allocated_fixed_jpy",
            "human_shadow_jpy",
            "electricity_jpy",
            "cac_jpy",
            "risk_reserve_jpy",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} は0以上の整数でなければなりません")
        if self.status not in {"complete", "incomplete"}:
            raise ValueError("status は complete または incomplete でなければなりません")

    @property
    def cash_total_jpy(self) -> int:
        return self.api_direct_jpy + self.electricity_jpy

    @property
    def economic_total_jpy(self) -> int:
        return (
            self.api_direct_jpy
            + self.allocated_fixed_jpy
            + self.human_shadow_jpy
            + self.electricity_jpy
            + self.cac_jpy
            + self.risk_reserve_jpy
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "api_direct_jpy": self.api_direct_jpy,
            "allocated_fixed_jpy": self.allocated_fixed_jpy,
            "human_shadow_jpy": self.human_shadow_jpy,
            "electricity_jpy": self.electricity_jpy,
            "cac_jpy": self.cac_jpy,
            "risk_reserve_jpy": self.risk_reserve_jpy,
            "cash_total_jpy": self.cash_total_jpy,
            "economic_total_jpy": self.economic_total_jpy,
            "status": self.status,
            "usage_ids": list(self.usage_ids),
        }


@dataclass(frozen=True, slots=True)
class InvoiceReconciliation:
    reconciliation_id: str
    estimated_jpy: int
    actual_jpy: int
    period_start: date
    period_end: date
    invoice_hash: str
    correction_entry_id: str | None = None

    def __post_init__(self) -> None:
        if not self.reconciliation_id.strip() or not self.invoice_hash.strip():
            raise ValueError("reconciliation_id/invoice_hash は空にできません")
        if self.estimated_jpy < 0 or self.actual_jpy < 0:
            raise ValueError("estimated_jpy/actual_jpy は0以上でなければなりません")
        if self.period_end < self.period_start:
            raise ValueError("period_end は period_start 以後でなければなりません")

    @property
    def variance_jpy(self) -> int:
        return self.actual_jpy - self.estimated_jpy

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "estimated_jpy": self.estimated_jpy,
            "actual_jpy": self.actual_jpy,
            "variance_jpy": self.variance_jpy,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "invoice_hash": self.invoice_hash,
            "correction_entry_id": self.correction_entry_id,
        }
