from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from vsm.errors import InvariantViolation, StreamConflict
from vsm.kernel.models import AppendResult, EventEnvelope, StoredEvent


class OperationalLedger(Protocol):
    def append(self, event: EventEnvelope, expected_stream_version: int) -> AppendResult: ...

    def page(self, after_cursor: int, limit: int) -> list[StoredEvent]: ...

    def stream(
        self, stream_id: str, after_stream_version: int, limit: int
    ) -> list[StoredEvent]: ...

    def put_blob(self, data: bytes) -> str: ...

    def get_blob(self, blob_ref: str) -> bytes: ...


class InMemoryOperationalLedger:
    """Deterministic LETHE contract double; no model or filesystem is involved."""

    def __init__(self, data_space_id: str) -> None:
        self.data_space_id = data_space_id
        self._events: list[StoredEvent] = []
        self._streams: dict[str, list[StoredEvent]] = defaultdict(list)
        self._idempotency: dict[str, StoredEvent] = {}
        self._event_ids: dict[str, StoredEvent] = {}
        self._blobs: dict[str, bytes] = {}

    def append(self, event: EventEnvelope, expected_stream_version: int) -> AppendResult:
        if event.data_space_id != self.data_space_id:
            raise InvariantViolation(
                f"event DataSpace {event.data_space_id} does not match {self.data_space_id}"
            )
        canonical = event.model_dump_json()
        duplicate = self._idempotency.get(event.idempotency_key)
        if duplicate is not None:
            if duplicate.event.model_dump_json() != canonical:
                raise InvariantViolation(
                    f"idempotency collision: {event.idempotency_key}"
                )
            return AppendResult(
                outcome="duplicate",
                cursor=duplicate.cursor,
                stream_version=duplicate.event.stream_version,
            )
        same_id = self._event_ids.get(event.event_id)
        if same_id is not None:
            if same_id.event.model_dump_json() != canonical:
                raise InvariantViolation(f"event id collision: {event.event_id}")
            return AppendResult(
                outcome="duplicate",
                cursor=same_id.cursor,
                stream_version=same_id.event.stream_version,
            )
        actual = len(self._streams[event.stream_id])
        if actual != expected_stream_version:
            raise StreamConflict(event.stream_id, expected_stream_version, actual)
        if event.stream_version != actual + 1:
            raise InvariantViolation(
                f"event stream version {event.stream_version} does not follow {actual}"
            )
        stored = StoredEvent(cursor=len(self._events) + 1, event=event)
        self._events.append(stored)
        self._streams[event.stream_id].append(stored)
        self._idempotency[event.idempotency_key] = stored
        self._event_ids[event.event_id] = stored
        return AppendResult(
            outcome="appended",
            cursor=stored.cursor,
            stream_version=event.stream_version,
        )

    def page(self, after_cursor: int, limit: int) -> list[StoredEvent]:
        if after_cursor < 0 or limit <= 0:
            raise InvariantViolation("cursor must be non-negative and limit positive")
        return self._events[after_cursor : after_cursor + limit]

    def stream(
        self, stream_id: str, after_stream_version: int, limit: int
    ) -> list[StoredEvent]:
        if after_stream_version < 0 or limit <= 0:
            raise InvariantViolation("version must be non-negative and limit positive")
        return self._streams[stream_id][
            after_stream_version : after_stream_version + limit
        ]

    def put_blob(self, data: bytes) -> str:
        import hashlib

        digest = hashlib.sha256(data).hexdigest()
        blob_ref = f"blob:sha256:{digest}"
        self._blobs[blob_ref] = bytes(data)
        return blob_ref

    def get_blob(self, blob_ref: str) -> bytes:
        try:
            return self._blobs[blob_ref]
        except KeyError as exc:
            raise InvariantViolation(f"blob not found: {blob_ref}") from exc
