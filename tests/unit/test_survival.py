from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from vsm.agents.runtime import AgentResult
from vsm.config import RunConfig
from vsm.roles import SystemRole
from vsm.survival.baseline import verify_baseline
from vsm.survival.ledger import EconomicLedger
from vsm.survival.metering import RuntimeMeter
from vsm.survival.models import LedgerEntry, LedgerEntryKind, UsageRecord
from vsm.survival.pricing import CostingError, FxRate, PriceBook, PriceProfile
from vsm.survival.reporting import DailyReportGenerator, ReportStore


UTC = timezone.utc


def dt(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=UTC)


def entry(*, entry_id: str, day: int, kind: LedgerEntryKind, amount: int, key: str) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        occurred_at=dt(day),
        booked_at=dt(day),
        kind=kind,
        category="test",
        signed_amount_jpy=amount,
        tax_jpy=0,
        original_amount=Decimal(abs(amount)),
        currency="JPY",
        fx_rate_id=None,
        source="test",
        source_id=entry_id,
        idempotency_key=key,
        actor_id="test",
    )


def test_ledger_is_append_only_and_survives_restart(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = EconomicLedger(path, tmp_path / "usage.jsonl")
    ledger.append(entry(entry_id="e1", day=1, kind=LedgerEntryKind.REVENUE, amount=1000, key="k1"))
    with pytest.raises(ValueError, match="already exists"):
        ledger.append(entry(entry_id="e2", day=1, kind=LedgerEntryKind.REVENUE, amount=1000, key="k1"))
    restored = EconomicLedger(path, tmp_path / "usage.jsonl")
    assert restored.balance_jpy() == 1000
    assert len(restored.entries()) == 1


def test_price_book_converts_tokens_and_fx_without_float():
    profile = PriceProfile(
        profile_id="p1",
        provider="openai",
        billing_mode="api",
        model="model-1",
        currency="USD",
        input_rate_per_million=Decimal("5"),
        output_rate_per_million=Decimal("10"),
        cache_read_rate_per_million=Decimal("1"),
        cache_write_rate_per_million=Decimal("2"),
        tool_rate=Decimal("0.5"),
        valid_from=date(2026, 7, 1),
    )
    fx = FxRate(
        rate_id="fx1",
        base_currency="USD",
        quote_currency="JPY",
        rate=Decimal("150"),
        effective_on=date(2026, 7, 1),
        source="owner invoice",
        retrieved_at=dt(1),
    )
    usage = UsageRecord(
        usage_id="u1",
        invocation_id="i1",
        run_id="run-1",
        node_id="node-1",
        provider="openai",
        backend="litellm",
        model="model-1",
        billing_mode="api",
        input_tokens=1_000_000,
        output_tokens=2_000_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        tool_units=0,
        started_at=dt(1),
        ended_at=dt(1, 12),
    )
    cost, resolved, resolved_fx = PriceBook([profile], [fx]).cost_usage(usage)
    assert cost == 3750
    assert resolved.profile_id == "p1"
    assert resolved_fx is fx


def test_missing_price_or_fx_fails_fast():
    usage = UsageRecord(
        usage_id="u1",
        invocation_id="i1",
        run_id="run-1",
        node_id="node-1",
        provider="openai",
        backend="litellm",
        model="model-1",
        billing_mode="api",
        input_tokens=1,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        tool_units=0,
        started_at=dt(1),
        ended_at=dt(1),
    )
    with pytest.raises(CostingError):
        PriceBook().cost_usage(usage)


def test_runtime_meter_records_unpriced_usage_explicitly(tmp_path):
    ledger = EconomicLedger(tmp_path / "ledger.jsonl", tmp_path / "usage.jsonl")
    meter = RuntimeMeter(
        ledger=ledger,
        price_book=PriceBook(),
        provider="unconfigured",
        billing_mode="unconfigured",
    )
    usage = meter.record_agent_result(
        "run-1",
        "node-1",
        AgentResult(
            text="ok",
            tokens_in=3,
            tokens_out=5,
            tokens_cache_read=1,
            latency_ms=10,
            model="model-1",
            backend="fake",
            session_ref=None,
        ),
        invocation_id="inv-1",
        started_at=dt(1),
    )
    assert usage.cost_status == "unpriced"
    assert len(ledger.usages()) == 1
    assert ledger.entries() == ()


def test_daily_report_has_null_reasons_and_idempotent_snapshot(tmp_path):
    ledger = EconomicLedger(tmp_path / "ledger.jsonl", tmp_path / "usage.jsonl")
    ledger.append(entry(entry_id="e1", day=1, kind=LedgerEntryKind.OWNER_CONTRIBUTION, amount=10000, key="k1"))
    ledger.append(entry(entry_id="e2", day=1, kind=LedgerEntryKind.EXPENSE, amount=-2500, key="k2"))
    ledger.append(entry(entry_id="e3", day=2, kind=LedgerEntryKind.REVENUE, amount=5000, key="k3"))
    generator = DailyReportGenerator(
        ledger=ledger,
        report_store=ReportStore(tmp_path / "reports.jsonl"),
    )
    report = generator.generate(date(2026, 7, 2))
    assert report["available_cash"] == 12500
    assert report["burn_30d_cash"] == 2500
    assert report["R_cash"] == 2.0
    assert len(report["daily_trend"]) == 30
    assert generator.generate(date(2026, 7, 2)) == report


def test_wave_zero_baseline_is_code_verified():
    snapshot = verify_baseline(RunConfig())
    assert snapshot.role_counts[SystemRole.S1_WORKER.value] == 0
    assert snapshot.role_counts[SystemRole.S5_POLICY.value] == 1
    assert snapshot.s1_hard_max == 1024
