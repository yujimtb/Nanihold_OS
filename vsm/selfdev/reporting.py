"""日次 self-development report の deterministic generator。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

from vsm.selfdev.artifacts import SelfDevArtifactLayout
from vsm.selfdev.store import SelfDevEventStore


class DailyReportError(RuntimeError):
    """日次 report の生成または既存 artifact の検証に失敗した。"""


class SelfDevDailyReporter:
    timezone = ZoneInfo("Asia/Tokyo")

    def __init__(self, store: SelfDevEventStore) -> None:
        self.store = store

    @staticmethod
    def _event_date(ts: str) -> date:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(SelfDevDailyReporter.timezone).date()

    def build(self, report_day: date) -> dict[str, Any]:
        events = [event for event in self.store.read_events() if self._event_date(event.ts) == report_day]
        proposals: dict[str, dict[str, Any]] = {}
        for event in events:
            proposal_id = event.payload.get("proposal_id")
            if not proposal_id:
                continue
            item = proposals.setdefault(str(proposal_id), {"proposal_id": str(proposal_id), "transitions": [], "failures": []})
            if event.event_type == "proposal_state_changed":
                item["transitions"].append(event.payload["to_state"])
                if event.payload["to_state"] in {"ABORTED", "REJECTED", "REJECTED_FINAL"}:
                    item["failures"].append(event.payload["reason"])
            if event.event_type in {"tool_failed", "consortium_aborted"}:
                item["failures"].append(event.payload.get("reason", "effect failed"))
        token_actual = sum(
            int(event.payload.get("tokens", 0))
            for event in events
            if event.event_type == "budget_consumed"
            and isinstance(event.payload.get("tokens", 0), (int, float))
        )
        states = [item["transitions"][-1] for item in proposals.values() if item["transitions"]]
        return {
            "schema_version": 1,
            "report_date": report_day.isoformat(),
            "timezone": "Asia/Tokyo",
            "proposals": list(proposals.values()),
            "consumption": {"tokens": token_actual},
            "counts": {
                "processed": len(proposals),
                "merge_ready": states.count("MERGE_READY"),
                "done": states.count("DONE"),
                "archived": states.count("ARCHIVED"),
            },
        }

    def write(self, report_day: date) -> tuple[Path, Path]:
        payload = self.build(report_day)
        layout: SelfDevArtifactLayout = self.store.layout
        json_path = layout.reports_dir / f"{report_day.isoformat()}.json"
        md_path = layout.reports_dir / f"{report_day.isoformat()}.md"
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        markdown = (
            f"# self-development report {report_day.isoformat()}\n\n"
            f"- timezone: Asia/Tokyo\n"
            f"- processed: {payload['counts']['processed']}\n"
            f"- tokens: {payload['consumption']['tokens']}\n"
            f"- MERGE_READY: {payload['counts']['merge_ready']}\n"
            f"- DONE: {payload['counts']['done']}\n"
            f"- ARCHIVED: {payload['counts']['archived']}\n"
        )
        for path, text in ((json_path, data), (md_path, markdown)):
            if path.exists():
                if path.read_text(encoding="utf-8") != text:
                    raise DailyReportError(f"同一日の日次 report が既に異なる内容で存在します: {path}")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        return json_path, md_path


DailyReportGenerator = SelfDevDailyReporter

__all__ = ["DailyReportError", "DailyReportGenerator", "SelfDevDailyReporter"]
