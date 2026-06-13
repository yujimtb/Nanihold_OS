"""Architecture-layer EventEnvelope helpers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventEnvelope(BaseModel):
    """Append-only source-of-truth envelope described by docs/architecture.md."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    run_id: str = Field(min_length=1, max_length=64)
    node_id: str | None = None
    stream_id: str = Field(min_length=1)
    stream_version: int = Field(ge=1)
    event_type: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    ts: str = Field(min_length=1)
    actor_type: str = Field(min_length=1)
    actor_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_event_dict(cls, event: dict[str, Any]) -> "EventEnvelope":
        """Normalise legacy five-field events into the v1 envelope shape."""

        run_id = event["run_id"]
        stream_id = event.get("stream_id") or event.get("node_id") or run_id
        return cls(
            event_id=event.get("event_id") or f"{run_id}:{event['seq']}",
            seq=event["seq"],
            run_id=run_id,
            node_id=event.get("node_id"),
            stream_id=stream_id,
            stream_version=event.get("stream_version") or event["seq"] + 1,
            event_type=event["event_type"],
            schema_version=event.get("schema_version") or 1,
            ts=event["ts"],
            actor_type=event.get("actor_type") or "system",
            actor_id=event.get("actor_id"),
            correlation_id=event.get("correlation_id") or run_id,
            causation_id=event.get("causation_id"),
            payload=dict(event.get("payload") or {}),
        )
