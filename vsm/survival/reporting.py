"""日次 survival report の projection と snapshot。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from vsm.survival.ledger import EconomicLedger
from vsm.survival.models import LedgerEntryKind


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SurvivalReport:
    report_date: date
    schema_version: int
    available_cash: int
    burn_30d_cash: int
    burn_30d_economic: int
    runway_months: float | None
    runway_days: float | None
    runway_reason: str | None
    R_cash: float | None
    R_cash_reason: str | None
    R_economic: float | None
    R_economic_reason: str | None
    owner_dependency: int
    unpriced_usage_count: int
    unpriced_usage_tokens: int
    daily_trend: tuple[dict[str, Any], ...]
    gross_margin_by_job: tuple[dict[str, Any], ...]
    input_event_range: dict[str, str | None]
    input_hash: str
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_date": self.report_date.isoformat(),
            "schema_version": self.schema_version,
            "available_cash": self.available_cash,
            "burn_30d_cash": self.burn_30d_cash,
            "burn_30d_economic": self.burn_30d_economic,
            "runway_months": self.runway_months,
            "runway_days": self.runway_days,
            "runway_reason": self.runway_reason,
            "R_cash": self.R_cash,
            "R_cash_reason": self.R_cash_reason,
            "R_economic": self.R_economic,
            "R_economic_reason": self.R_economic_reason,
            "owner_dependency": self.owner_dependency,
            "unpriced_usage": {
                "count": self.unpriced_usage_count,
                "tokens": self.unpriced_usage_tokens,
            },
            "daily_trend": list(self.daily_trend),
            "gross_margin_by_job": list(self.gross_margin_by_job),
            "input_event_range": self.input_event_range,
            "input_hash": self.input_hash,
            "generated_at": self.generated_at,
        }


class ReportStore:
    """日付 idempotency を持つ append-only report snapshot。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._reports: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    raise ValueError("report store に空行があります")
                report = json.loads(line)
                key = report.get("report_date")
                if not isinstance(key, str) or key in self._reports:
                    raise ValueError("report store の日付または重複が不正です")
                self._reports[key] = report

    def get(self, report_date: date) -> dict[str, Any] | None:
        value = self._reports.get(report_date.isoformat())
        return dict(value) if value is not None else None

    def append(self, report: SurvivalReport) -> dict[str, Any]:
        payload = report.to_dict()
        key = report.report_date.isoformat()
        existing = self._reports.get(key)
        if existing is not None:
            return dict(existing)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        markdown_dir = self.path.parent / "markdown"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = markdown_dir / f"survival-{key}.md"
        with markdown_path.open("w", encoding="utf-8") as handle:
            handle.write(DailyReportGenerator.markdown(payload))
            handle.flush()
            os.fsync(handle.fileno())
        self._reports[key] = payload
        return dict(payload)

    def all(self) -> list[dict[str, Any]]:
        return [dict(self._reports[key]) for key in sorted(self._reports)]


class DailyReportGenerator:
    def __init__(self, *, ledger: EconomicLedger, report_store: ReportStore) -> None:
        self.ledger = ledger
        self.report_store = report_store

    def generate(self, report_date: date) -> dict[str, Any]:
        existing = self.report_store.get(report_date)
        if existing is not None:
            return existing
        end = report_date + timedelta(days=1)
        start = report_date - timedelta(days=29)
        entries = [
            entry
            for entry in self.ledger.entries()
            if entry.occurred_at.date() <= report_date
        ]
        window_entries = [
            entry for entry in entries if start <= entry.occurred_at.date() <= report_date
        ]
        available_cash = sum(entry.signed_amount_jpy for entry in entries)
        expenses = [entry for entry in window_entries if entry.kind is LedgerEntryKind.EXPENSE]
        revenue = [entry for entry in window_entries if entry.kind is LedgerEntryKind.REVENUE]
        burn_cash = sum(-entry.signed_amount_jpy for entry in expenses)
        burn_economic = burn_cash
        revenue_jpy = sum(entry.signed_amount_jpy for entry in revenue)
        runway = available_cash / burn_cash if burn_cash else None
        runway_reason = None if burn_cash else "committed monthly burn が0円のため算出不可"
        r_cash = revenue_jpy / burn_cash if burn_cash else None
        r_cash_reason = None if burn_cash else "cash operating outflow が0円のため算出不可"
        r_economic = revenue_jpy / burn_economic if burn_economic else None
        r_economic_reason = None if burn_economic else "fully loaded economic cost が0円のため算出不可"
        owner_dependency = sum(
            entry.signed_amount_jpy
            for entry in entries
            if entry.kind is LedgerEntryKind.OWNER_CONTRIBUTION
            or entry.category.startswith("owner_paid")
        )
        unpriced = [
            usage
            for usage in self.ledger.usages()
            if usage.started_at.date() <= report_date and usage.cost_status != "priced"
        ]
        daily_trend = tuple(self._daily_trend(report_date))
        input_values = [entry.to_dict() for entry in entries]
        input_values.extend(usage.to_dict() for usage in self.ledger.usages())
        report = SurvivalReport(
            report_date=report_date,
            schema_version=1,
            available_cash=available_cash,
            burn_30d_cash=burn_cash,
            burn_30d_economic=burn_economic,
            runway_months=runway,
            runway_days=runway * 30 if runway is not None else None,
            runway_reason=runway_reason,
            R_cash=r_cash,
            R_cash_reason=r_cash_reason,
            R_economic=r_economic,
            R_economic_reason=r_economic_reason,
            owner_dependency=owner_dependency,
            unpriced_usage_count=len(unpriced),
            unpriced_usage_tokens=sum(usage.total_tokens for usage in unpriced),
            daily_trend=daily_trend,
            gross_margin_by_job=tuple(self._gross_margin(window_entries)),
            input_event_range={
                "from": min((entry.occurred_at.isoformat() for entry in entries), default=None),
                "to": max((entry.occurred_at.isoformat() for entry in entries), default=None),
            },
            input_hash=_json_hash(input_values),
            generated_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
        )
        return self.report_store.append(report)

    def _daily_trend(self, report_date: date) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for offset in range(29, -1, -1):
            day = report_date - timedelta(days=offset)
            entries = [entry for entry in self.ledger.entries() if entry.occurred_at.date() == day]
            result.append(
                {
                    "date": day.isoformat(),
                    "revenue_jpy": sum(
                        entry.signed_amount_jpy
                        for entry in entries
                        if entry.kind is LedgerEntryKind.REVENUE
                    ),
                    "expense_jpy": sum(
                        -entry.signed_amount_jpy
                        for entry in entries
                        if entry.kind is LedgerEntryKind.EXPENSE
                    ),
                }
            )
        return result

    @staticmethod
    def _gross_margin(entries: list) -> list[dict[str, Any]]:
        by_job: dict[str, int] = {}
        for entry in entries:
            key = entry.opportunity_id or entry.run_id or "unassigned"
            by_job[key] = by_job.get(key, 0) + entry.signed_amount_jpy
        return [
            {"job_id": job_id, "gross_margin_jpy": amount}
            for job_id, amount in sorted(by_job.items())
        ]

    @staticmethod
    def markdown(report: dict[str, Any]) -> str:
        def yen(value: int | None) -> str:
            return "未算出" if value is None else f"¥{value:,}"

        ratio = lambda value: "未算出" if value is None else f"{value:.2f}"
        lines = [
            f"# Survival Report — {report['report_date']}",
            "",
            f"- Available cash: {yen(report['available_cash'])}",
            f"- Burn (30d cash): {yen(report['burn_30d_cash'])}",
            f"- Burn (30d economic): {yen(report['burn_30d_economic'])}",
            f"- Runway: {ratio(report['runway_months'])} months",
            f"- R cash: {ratio(report['R_cash'])}",
            f"- R economic: {ratio(report['R_economic'])}",
            f"- Owner dependency: {yen(report['owner_dependency'])}",
            f"- Unpriced usage: {report['unpriced_usage']['count']} records / {report['unpriced_usage']['tokens']} tokens",
            "",
            "日次推移は JSON snapshot の `daily_trend` を正本とします。",
        ]
        return "\n".join(lines) + "\n"


class DailyReportScheduler:
    """06:30 Asia/Tokyo 実行と再起動 catch-up の決定論的な境界。"""

    def __init__(
        self,
        *,
        generator: DailyReportGenerator,
        report_time: str = "06:30",
        timezone_name: str = "Asia/Tokyo",
    ) -> None:
        if len(report_time) != 5 or report_time[2] != ":":
            raise ValueError("report_time は HH:MM 形式でなければなりません")
        hour, minute = (int(part) for part in report_time.split(":", 1))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("report_time が不正です")
        self.generator = generator
        self.report_time = time(hour, minute)
        self.timezone = ZoneInfo(timezone_name)

    def catch_up(self, now: datetime) -> dict[str, Any] | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("scheduler now は timezone-aware でなければなりません")
        local_now = now.astimezone(self.timezone)
        if local_now.timetz().replace(tzinfo=None) < self.report_time:
            return None
        return self.generator.generate(local_now.date() - timedelta(days=1))
