"""自己開発 controller Event Log の durable store。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vsm.clock import Clock, SystemClock
from vsm.eventlog.schema import Event, validate_event_payload
from vsm.eventlog.writer import EventLogWriter
from vsm.selfdev.artifacts import SelfDevArtifactLayout
from vsm.selfdev.projection import ProposalProjection, replay_projections


class SelfDevEventStore:
    run_id = "selfdev-controller"

    def __init__(self, root: Path, *, clock: Clock | None = None) -> None:
        self.layout = SelfDevArtifactLayout(root)
        self.clock = clock or SystemClock()
        self._writer: EventLogWriter | None = None

    @property
    def events_path(self) -> Path:
        return self.layout.events_path

    async def start(self) -> None:
        if self._writer is not None:
            return
        self.layout.controller_dir.mkdir(parents=True, exist_ok=True)
        self._writer = EventLogWriter(
            self.run_id,
            self.events_path,
            self.clock,
            durability="durable",
            strict_recovery=True,
        )
        await self._writer.start()

    async def stop(self) -> None:
        if self._writer is not None:
            await self._writer.stop()
            self._writer = None

    async def append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        proposal_id: str,
        actor_type: str = "controller",
        actor_id: str | None = None,
        causation_id: str | None = None,
        expected_stream_version: int | None = None,
        schema_version: int = 1,
    ) -> Event:
        if self._writer is None:
            raise RuntimeError("SelfDevEventStore.start() 前に append できません")
        event = await self._writer.append(
            event_type,
            payload,
            stream_id=f"selfdev:proposal:{proposal_id}",
            actor_type=actor_type,
            actor_id=actor_id,
            correlation_id=proposal_id,
            causation_id=causation_id,
            expected_stream_version=expected_stream_version,
            schema_version=schema_version,
        )
        if event is None:
            raise RuntimeError("selfdev Event Store は durable event を返さなければなりません")
        return event

    def read_events(self) -> list[Event]:
        events: list[Event] = []
        expected_seq = 0
        stream_versions: dict[str, int] = {}
        with self.events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.endswith("\n"):
                    raise ValueError("自己開発 Event Log の末尾が torn です")
                if not line.strip():
                    raise ValueError("自己開発 Event Log に空行があります")
                event = Event.model_validate(json.loads(line))
                validate_event_payload(
                    event.event_type, event.payload, schema_version=event.schema_version
                )
                if event.seq != expected_seq:
                    raise ValueError("自己開発 Event Log の seq が連続していません")
                expected_seq += 1
                stream = event.stream_id or event.node_id or self.run_id
                expected_version = stream_versions.get(stream, 0) + 1
                if event.stream_version != expected_version:
                    raise ValueError("自己開発 Event Log の stream_version が連続していません")
                stream_versions[stream] = expected_version
                events.append(event)
        return events

    def replay(self) -> dict[str, ProposalProjection]:
        return replay_projections(self.read_events())

    def projection(self, proposal_id: str) -> ProposalProjection | None:
        return self.replay().get(proposal_id)


EventLogStore = SelfDevEventStore
RunStore = SelfDevEventStore

__all__ = ["EventLogStore", "RunStore", "SelfDevEventStore"]
