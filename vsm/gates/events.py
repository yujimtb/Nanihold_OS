"""Event_Log integration for trusted gate reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from vsm.eventlog.writer import EventLogWriter


async def record_gate_report_generated(
    eventlog: EventLogWriter,
    report: Mapping[str, Any],
) -> None:
    """Append a minimal ``gate_report_generated`` event for a Run context.

    A standalone GateRunner has no Run Event_Log and therefore does not call
    this function.  The full report remains in the report file; the event only
    records stable metadata and per-gate statuses so sensitive command output
    is not copied into the append-only log.
    """

    gates = report["gates"]
    if not isinstance(gates, Mapping):
        raise TypeError("report['gates'] must be a mapping")
    gate_statuses = {
        str(name): str(detail["status"])
        for name, detail in gates.items()
        if isinstance(detail, Mapping)
    }
    if report.get("schema_version") == 2:
        required = (
            "proposal_id", "implementation_run_id", "gate_attempt", "report_ref",
            "scope_sha256", "candidate_diff_sha256",
        )
        missing = [name for name in required if name not in report]
        if missing:
            raise ValueError("GateReport v2 の event metadata が不足しています: " + ", ".join(missing))
        await eventlog.append(
            "gate_report_generated",
            {
                "proposal_id": str(report["proposal_id"]),
                "implementation_run_id": str(report["implementation_run_id"]),
                "gate_attempt": int(report["gate_attempt"]),
                "report_ref": str(report["report_ref"]),
                "status": str(report["status"]),
                "gate_statuses": gate_statuses,
                "scope_sha256": str(report["scope_sha256"]),
                "candidate_diff_sha256": str(report["candidate_diff_sha256"]),
            },
            actor_type="trusted_gate_runner",
            schema_version=2,
        )
        return
    await eventlog.append(
        "gate_report_generated",
        {
            "report_path": str(report["report_path"]),
            "worktree": str(report["worktree"]),
            "base": str(report["base"]),
            "status": str(report["status"]),
            "gate_statuses": gate_statuses,
        },
        actor_type="trusted_gate_runner",
    )
