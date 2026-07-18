"""設定に従って disabled / dry-run / live を切り替える Run bridge。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from vsm.config import LetheConfig
from vsm.eventlog.reader import read_all
from vsm.lethe_bridge.client import LetheClient, LetheTransport
from vsm.lethe_bridge.exporter import build_accounting_record, build_memory_records
from vsm.lethe_bridge.models import (
    SUPPLEMENTAL_RECORD_ADAPTER,
    SupplementalRecord,
)


class LetheReader(Protocol):
    def search(self, query: str) -> list[SupplementalRecord]: ...


LetheContextInjector = Callable[
    [Sequence[SupplementalRecord]], None | Awaitable[None]
]


class LetheBridge:
    """Run hook から利用する単一の LETHE 接続面。"""

    def __init__(
        self,
        *,
        config: LetheConfig,
        transport: LetheTransport | None = None,
    ) -> None:
        self.config = config
        self._client: LetheClient | None = None
        if config.enabled and config.mode == "live":
            if config.endpoint is None or config.token is None:
                raise ValueError("enabled live LETHE config requires endpoint and token")
            self._client = LetheClient(
                endpoint=config.endpoint,
                token=config.token,
                transport=transport,
            )

    def search(self, query: str) -> list[SupplementalRecord]:
        if not self.config.enabled:
            return []
        cleaned = query.strip()
        if not cleaned:
            raise ValueError("LETHE search query must not be empty")
        if self.config.mode == "live":
            if self._client is None:
                raise RuntimeError("live LETHE client is not initialized")
            return self._client.search(cleaned)
        return self._search_dry_run(cleaned)

    async def inject_context(
        self, query: str, injector: LetheContextInjector
    ) -> list[SupplementalRecord]:
        if not self.config.enabled:
            return []
        records = await asyncio.to_thread(self.search, query)
        result = injector(records)
        if inspect.isawaitable(result):
            await result
        return records

    def export_run(
        self,
        *,
        run_id: str,
        ended_at: str,
        events_path: Path,
        nodes: Mapping[str, Any],
        node_run_states: Mapping[tuple[str, str], Any],
        run_consumption: Mapping[str, float],
    ) -> list[SupplementalRecord]:
        if not self.config.enabled:
            return []
        events = read_all(events_path)
        records: list[SupplementalRecord] = [
            build_accounting_record(
                run_id=run_id,
                ended_at=ended_at,
                events=events,
                nodes=nodes,
                node_run_states=node_run_states,
                run_consumption=run_consumption,
            ),
            *build_memory_records(run_id=run_id, events=events),
        ]
        if self.config.mode == "dry-run":
            self._append_dry_run(records)
        else:
            if self._client is None:
                raise RuntimeError("live LETHE client is not initialized")
            for record in records:
                self._client.write_supplemental(record)
        return records

    def _append_dry_run(self, records: Sequence[SupplementalRecord]) -> None:
        path = self.config.dry_run_path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_records: dict[str, SupplementalRecord] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as existing:
                for line_number, line in enumerate(existing, start=1):
                    if not line.strip():
                        raise ValueError(
                            "LETHE dry-run file contains a blank line at "
                            f"{line_number}"
                        )
                    record = SUPPLEMENTAL_RECORD_ADAPTER.validate_json(line)
                    if record.record_id in existing_records:
                        raise ValueError(
                            f"duplicate LETHE dry-run record_id: {record.record_id}"
                        )
                    existing_records[record.record_id] = record
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                existing = existing_records.get(record.record_id)
                if existing is not None:
                    if existing != record:
                        raise ValueError(
                            "LETHE dry-run record_id content conflict: "
                            f"{record.record_id}"
                        )
                    continue
                handle.write(record.model_dump_json() + "\n")
                existing_records[record.record_id] = record

    def _search_dry_run(self, query: str) -> list[SupplementalRecord]:
        path = self.config.dry_run_path
        if not path.exists():
            return []
        words = tuple(word.casefold() for word in query.split() if word)
        records: list[SupplementalRecord] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise ValueError(
                        f"LETHE dry-run file contains a blank line at {line_number}"
                    )
                record = SUPPLEMENTAL_RECORD_ADAPTER.validate_json(line)
                searchable = record.text.casefold()
                if all(word in searchable for word in words):
                    records.append(record)
        return records
