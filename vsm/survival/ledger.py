"""append-only の円建て operational ledger。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Iterable

from vsm.survival.models import LedgerEntry, UsageRecord


class EconomicLedger:
    """Ledger と Usage を別 JSONL に保持する再起動耐性ストア。"""

    def __init__(self, path: Path, usage_path: Path | None = None) -> None:
        self.path = path
        self.usage_path = usage_path or path.with_name(f"{path.stem}.usage.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._entries: list[LedgerEntry] = []
        self._usages: list[UsageRecord] = []
        self._entry_keys: set[str] = set()
        self._usage_keys: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    raise ValueError("ledger に空行があります")
                try:
                    entry = LedgerEntry.from_dict(json.loads(line))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("ledger の行が不正です") from exc
                if entry.idempotency_key in self._entry_keys:
                    raise ValueError("ledger に重複 idempotency_key があります")
                self._entry_keys.add(entry.idempotency_key)
                self._entries.append(entry)
        if self.usage_path.exists():
            for line in self.usage_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    raise ValueError("usage ledger に空行があります")
                try:
                    usage = UsageRecord.from_dict(json.loads(line))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError("usage ledger の行が不正です") from exc
                if usage.usage_id in self._usage_keys:
                    raise ValueError("usage ledger に重複 usage_id があります")
                self._usage_keys.add(usage.usage_id)
                self._usages.append(usage)

    @staticmethod
    def _append_jsonl(path: Path, payload: dict) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        with self._lock:
            if entry.idempotency_key in self._entry_keys:
                raise ValueError(f"ledger idempotency_key already exists: {entry.idempotency_key}")
            self._append_jsonl(self.path, entry.to_dict())
            self._entries.append(entry)
            self._entry_keys.add(entry.idempotency_key)
            return entry

    append_entry = append

    def record_usage(self, usage: UsageRecord) -> UsageRecord:
        with self._lock:
            if usage.usage_id in self._usage_keys:
                raise ValueError(f"usage_id already exists: {usage.usage_id}")
            self._append_jsonl(self.usage_path, usage.to_dict())
            self._usages.append(usage)
            self._usage_keys.add(usage.usage_id)
            return usage

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)

    def usages(self) -> tuple[UsageRecord, ...]:
        return tuple(self._usages)

    def total_jpy(self, *, kinds: Iterable[str] | None = None) -> int:
        allowed = set(kinds) if kinds is not None else None
        return sum(
            entry.signed_amount_jpy
            for entry in self._entries
            if allowed is None or entry.kind.value in allowed
        )

    def balance_jpy(self) -> int:
        return self.total_jpy()

    def latest_booked_at(self) -> datetime | None:
        if not self._entries:
            return None
        return max(entry.booked_at for entry in self._entries)
