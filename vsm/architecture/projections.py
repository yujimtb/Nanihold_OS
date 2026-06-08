"""Projection checkpoint primitives."""

from __future__ import annotations

from dataclasses import dataclass, field

from vsm.architecture.events import EventEnvelope


@dataclass
class ProjectionCheckpoint:
    """Idempotent checkpoint for an Event_Log projection."""

    projection_name: str
    projection_version: int
    last_seq: int = -1
    last_event_id: str | None = None
    applied_event_ids: set[str] = field(default_factory=set)

    def should_apply(self, event: EventEnvelope) -> bool:
        return event.seq > self.last_seq and event.event_id not in self.applied_event_ids

    def mark_applied(self, event: EventEnvelope) -> None:
        if self.should_apply(event):
            self.last_seq = event.seq
            self.last_event_id = event.event_id
            self.applied_event_ids.add(event.event_id)
