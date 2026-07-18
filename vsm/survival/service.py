"""Web API / runtime hook が共有する survival application service。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from vsm.config import RunConfig
from vsm.survival.baseline import verify_baseline
from vsm.survival.config import SurvivalConfig, load_survival_config
from vsm.survival.ledger import EconomicLedger
from vsm.survival.metering import RuntimeMeter
from vsm.survival.pricing import PriceBook, load_price_book
from vsm.survival.reporting import DailyReportGenerator, DailyReportScheduler, ReportStore


class SurvivalService:
    """再起動後も同じ ledger/report snapshot を読む単一サービス。"""

    def __init__(
        self,
        *,
        config: SurvivalConfig | None = None,
        run_config: RunConfig | None = None,
        price_book: PriceBook | None = None,
    ) -> None:
        self.config = config or load_survival_config()
        self.ledger = EconomicLedger(self.config.ledger_path, self.config.usage_path)
        self.report_store = ReportStore(self.config.report_path)
        self.reports = DailyReportGenerator(
            ledger=self.ledger,
            report_store=self.report_store,
        )
        self.scheduler = DailyReportScheduler(
            generator=self.reports,
            report_time=self.config.daily_report_time,
            timezone_name=self.config.timezone_name,
        )
        if price_book is not None:
            self.price_book = price_book
        elif self.config.pricing_path.exists():
            self.price_book = load_price_book(self.config.pricing_path)
        else:
            self.price_book = PriceBook()
        self.meter = RuntimeMeter(
            ledger=self.ledger,
            price_book=self.price_book,
            provider="unconfigured",
            billing_mode="unconfigured",
        )
        self._run_config = run_config or RunConfig()

    def baseline(self) -> dict[str, Any]:
        return verify_baseline(self._run_config).to_dict()

    def dashboard(self, report_date: date | None = None) -> dict[str, Any]:
        target = report_date or date.today()
        report = self.reports.generate(target)
        return {
            "schema_version": 1,
            "as_of": target.isoformat(),
            "baseline": self.baseline(),
            "safety": self.config.safety.to_dict(),
            "report": report,
            "daily_trend": report["daily_trend"],
            "ledger": {
                "entry_count": len(self.ledger.entries()),
                "usage_count": len(self.ledger.usages()),
                "balance_jpy": self.ledger.balance_jpy(),
                "recent_entries": [entry.to_dict() for entry in self.ledger.entries()[-20:]],
            },
        }

    def record_entry(self, entry) -> dict[str, Any]:
        return self.ledger.append(entry).to_dict()

    def report(self, report_date: date) -> dict[str, Any]:
        return self.reports.generate(report_date)

    def catch_up(self, now) -> dict[str, Any] | None:
        return self.scheduler.catch_up(now)
