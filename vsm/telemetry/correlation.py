"""OpenTelemetry/Event_Log correlation values."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TelemetryCorrelation:
    event_id: str | None = None
    run_id: str | None = None
    node_id: str | None = None
    tool_invocation_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None

    def as_attributes(self) -> dict[str, str]:
        return {k: v for k, v in self.__dict__.items() if v is not None}
