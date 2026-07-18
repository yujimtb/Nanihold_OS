"""AgentRuntime の最小 metering 接点。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from vsm.agents.runtime import AgentResult
from vsm.survival.ledger import EconomicLedger
from vsm.survival.models import LedgerEntry, LedgerEntryKind, UsageRecord
from vsm.survival.pricing import CostingError, PriceBook


class RuntimeMeter:
    """AgentResult を UsageRecord と円の expense entry へ変換する。

    Price/FX が未確定の場合、実行を別経路へ流さず ``unpriced`` として明示記録する。
    見積・受注側はこの状態をエラーとして扱える。
    """

    def __init__(
        self,
        *,
        ledger: EconomicLedger,
        price_book: PriceBook,
        provider: str,
        billing_mode: str,
    ) -> None:
        if not provider.strip() or not billing_mode.strip():
            raise ValueError("provider/billing_mode は空にできません")
        self.ledger = ledger
        self.price_book = price_book
        self.provider = provider
        self.billing_mode = billing_mode

    def record_agent_result(
        self,
        run_id: str,
        node_id: str,
        result: AgentResult,
        *,
        invocation_id: str | None = None,
        started_at: datetime | None = None,
        event_id: str | None = None,
    ) -> UsageRecord:
        ended_at = datetime.now(timezone.utc)
        started = started_at or ended_at - timedelta(milliseconds=result.latency_ms)
        invocation = invocation_id or str(uuid4())
        usage = UsageRecord(
            usage_id=invocation,
            invocation_id=invocation,
            run_id=run_id,
            node_id=node_id,
            provider=self.provider,
            backend=result.backend,
            model=result.model,
            billing_mode=self.billing_mode,
            input_tokens=result.tokens_in,
            output_tokens=result.tokens_out,
            cache_read_tokens=result.tokens_cache_read,
            cache_write_tokens=0,
            tool_units="0",
            started_at=started,
            ended_at=ended_at,
            wall_clock_ms=result.latency_ms,
            event_id=event_id,
        )
        try:
            cost_jpy, profile, fx = self.price_book.cost_usage(usage)
        except CostingError as exc:
            usage = replace(
                usage,
                cost_status="unpriced",
                costing_error=str(exc),
            )
            self.ledger.record_usage(usage)
            return usage
        usage = replace(
            usage,
            price_profile_id=profile.profile_id,
            cost_status="priced",
            cost_jpy=cost_jpy,
        )
        self.ledger.record_usage(usage)
        original_amount = (
            Decimal(cost_jpy)
            if profile.currency.upper() == "JPY"
            else (
                (
                    Decimal(usage.input_tokens) * profile.input_rate_per_million
                    + Decimal(usage.output_tokens) * profile.output_rate_per_million
                    + Decimal(usage.cache_read_tokens) * profile.cache_read_rate_per_million
                    + Decimal(usage.cache_write_tokens) * profile.cache_write_rate_per_million
                ) / Decimal(1_000_000)
                + usage.tool_units * profile.tool_rate
                + Decimal(usage.wall_clock_ms) / Decimal(3_600_000) * profile.wall_clock_rate_per_hour
            )
        )
        entry = LedgerEntry(
            entry_id=f"cost:{usage.usage_id}",
            occurred_at=usage.ended_at,
            booked_at=usage.ended_at,
            kind=LedgerEntryKind.EXPENSE,
            category="runtime_usage",
            signed_amount_jpy=-cost_jpy,
            tax_jpy=0,
            original_amount=original_amount,
            currency=profile.currency,
            fx_rate_id=fx.rate_id if fx is not None else None,
            source="runtime_meter",
            source_id=usage.usage_id,
            idempotency_key=f"usage-cost:{usage.usage_id}",
            run_id=run_id,
            actor_id=node_id,
            metadata={"provider": usage.provider, "backend": usage.backend, "model": usage.model},
        )
        self.ledger.append(entry)
        return usage
