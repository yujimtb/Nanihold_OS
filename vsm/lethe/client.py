from __future__ import annotations

import json
import uuid
from datetime import UTC

import httpx

from vsm.errors import (
    InvariantViolation,
    ReconciliationRequired,
    StreamConflict,
)
from vsm.kernel.models import AppendResult, EventEnvelope, StoredEvent
from vsm.activation.reorientation import HistoryPage


class LetheOperationalLedger:
    """Nanihold's only production Event Ledger adapter."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        data_space_id: str,
        timeout_seconds: float,
        max_page_size: int,
    ) -> None:
        if (
            not base_url
            or not bearer_token
            or not data_space_id
            or timeout_seconds <= 0
            or max_page_size <= 0
        ):
            raise InvariantViolation("LETHE connection fields must be explicit and valid")
        self.data_space_id = data_space_id
        self.max_page_size = max_page_size
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def _raise(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            detail = response.json()
        except ValueError:
            detail = response.text
        raise InvariantViolation(
            f"LETHE HTTP {response.status_code}: {json.dumps(detail, ensure_ascii=False)}"
        )

    def append(self, event: EventEnvelope, expected_stream_version: int) -> AppendResult:
        if event.data_space_id != self.data_space_id:
            raise InvariantViolation("event DataSpace does not match configured LETHE Lake")
        occurred_at = event.occurred_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        canonical = event.model_dump_json()
        observation_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"nanihold:{event.event_id}")
        )
        request = {
            "event": {
                "event_id": event.event_id,
                "data_space_id": event.data_space_id,
                "stream_id": event.stream_id,
                "stream_version": event.stream_version,
                "event_type": event.event_type,
                "occurred_at": occurred_at,
                "actor_type": event.actor_type,
                "actor_id": event.actor_id,
                "correlation_id": event.correlation_id,
                "causation_id": event.causation_id,
                "observation": {
                    "id": observation_id,
                    "schema": "schema:nanihold-operational-event",
                    "schema_version": "1.0.0",
                    "observer": "obs:nanihold-kernel",
                    "source_system": "sys:nanihold",
                    "actor": event.actor_id,
                    "authority_model": "lake_authoritative",
                    "capture_model": "event",
                    "subject": event.stream_id,
                    "target": None,
                    "payload": event.payload,
                    "attachments": [],
                    "published": occurred_at,
                    # The complete retry envelope must remain byte-equivalent for
                    # LETHE's content-aware idempotency collision check.
                    "recorded_at": occurred_at,
                    "consent": None,
                    "idempotency_key": event.idempotency_key,
                    "meta": {
                        "canonical_json": canonical,
                        "source_container": "nanihold",
                        "data_space_id": event.data_space_id,
                        "event_id": event.event_id,
                    },
                },
            },
            "expected_stream_version": expected_stream_version,
        }
        try:
            response = self._client.post(
                "/api/operational-events", json={"requests": [request]}
            )
        except httpx.TransportError:
            return self._reconcile_event(event)
        if response.status_code == 409:
            return self._reconcile_event(event)
        self._raise(response)
        outcomes = response.json()["outcomes"]
        if len(outcomes) != 1:
            raise InvariantViolation("LETHE returned an invalid append outcome count")
        outcome = outcomes[0]
        if outcome["outcome"] == "version_conflict":
            raise StreamConflict(
                event.stream_id, outcome["expected"], outcome["actual"]
            )
        if outcome["outcome"] not in ("appended", "duplicate"):
            raise InvariantViolation(f"unknown LETHE append outcome: {outcome!r}")
        return AppendResult.model_validate(outcome)

    def _reconcile_event(self, expected: EventEnvelope) -> AppendResult:
        try:
            response = self._client.get(
                f"/api/operational-events/{expected.event_id}"
            )
        except httpx.TransportError as exc:
            raise ReconciliationRequired(
                f"LETHE outcome for event {expected.event_id} is unreachable"
            ) from exc
        if response.status_code == 404:
            raise ReconciliationRequired(
                f"LETHE did not persist event {expected.event_id}; "
                "the explicit command may now be retried"
            )
        self._raise(response)
        stored = self._stored(response.json())
        expected_intent = expected.model_dump(
            mode="json", exclude={"occurred_at"}
        )
        stored_intent = stored.event.model_dump(
            mode="json", exclude={"occurred_at"}
        )
        if stored_intent != expected_intent:
            raise InvariantViolation(
                f"LETHE event identity collision: {expected.event_id}"
            )
        return AppendResult(
            outcome="duplicate",
            cursor=stored.cursor,
            stream_version=stored.event.stream_version,
        )

    @staticmethod
    def _stored(raw: dict[str, object]) -> StoredEvent:
        outer = raw["event"]
        if not isinstance(outer, dict):
            raise InvariantViolation("LETHE stored event is malformed")
        observation = outer.get("observation")
        if not isinstance(observation, dict):
            raise InvariantViolation("LETHE stored Observation is malformed")
        event = EventEnvelope(
            event_id=outer["event_id"],
            data_space_id=outer["data_space_id"],
            stream_id=outer["stream_id"],
            stream_version=outer["stream_version"],
            event_type=outer["event_type"],
            occurred_at=outer["occurred_at"],
            actor_type=outer["actor_type"],
            actor_id=outer.get("actor_id"),
            correlation_id=outer.get("correlation_id"),
            causation_id=outer.get("causation_id"),
            idempotency_key=observation["idempotency_key"],
            payload=observation["payload"],
        )
        return StoredEvent(cursor=raw["cursor"], event=event)

    def page(self, after_cursor: int, limit: int) -> list[StoredEvent]:
        requested_limit = min(limit, self.max_page_size)
        response = self._client.get(
            "/api/operational-events",
            params={"after_cursor": after_cursor, "limit": requested_limit},
        )
        self._raise(response)
        return [self._stored(item) for item in response.json()["events"]]

    def stream(
        self, stream_id: str, after_stream_version: int, limit: int
    ) -> list[StoredEvent]:
        requested_limit = min(limit, self.max_page_size)
        response = self._client.get(
            f"/api/operational-streams/{stream_id}",
            params={
                "after_stream_version": after_stream_version,
                "limit": requested_limit,
            },
        )
        self._raise(response)
        return [self._stored(item) for item in response.json()["events"]]

    def put_blob(self, data: bytes) -> str:
        response = self._client.post(
            "/api/operational-blobs",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        self._raise(response)
        blob_ref = response.json()
        if not isinstance(blob_ref, str) or not blob_ref.startswith("blob:sha256:"):
            raise InvariantViolation("LETHE returned an invalid BlobRef")
        return blob_ref

    def get_blob(self, blob_ref: str) -> bytes:
        prefix = "blob:sha256:"
        if not blob_ref.startswith(prefix):
            raise InvariantViolation("invalid BlobRef")
        response = self._client.get(f"/api/operational-blobs/{blob_ref[len(prefix):]}")
        self._raise(response)
        return response.content


class LetheHistoryClient:
    """Production HistoryReader backed only by LETHE's indexed projection."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        data_space_id: str,
        timeout_seconds: float,
        max_result_bytes: int,
    ) -> None:
        if (
            not base_url
            or not bearer_token
            or not data_space_id
            or timeout_seconds <= 0
            or max_result_bytes <= 0
        ):
            raise InvariantViolation("LETHE history connection must be explicit")
        self.data_space_id = data_space_id
        self.max_result_bytes = max_result_bytes
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def _query(
        self,
        operation: str,
        argument: str | None,
        page_cursor: str | None,
    ) -> HistoryPage:
        response = self._client.post(
            "/api/history/query",
            json={
                "data_space_id": self.data_space_id,
                "operation": operation,
                "argument": argument,
                "page_cursor": page_cursor,
                "max_result_bytes": self.max_result_bytes,
            },
        )
        if not response.is_success:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise InvariantViolation(
                f"LETHE history HTTP {response.status_code}: "
                f"{json.dumps(detail, ensure_ascii=False)}"
            )
        return HistoryPage.model_validate(response.json())

    def list_sessions(self, *, page_cursor: str | None) -> HistoryPage:
        return self._query("list_sessions", None, page_cursor)

    def read_timeline(
        self, session_id: str, *, page_cursor: str | None
    ) -> HistoryPage:
        return self._query("read_timeline", session_id, page_cursor)

    def read_raw(self, message_id: str, *, page_cursor: str | None) -> HistoryPage:
        return self._query("read_raw", message_id, page_cursor)

    def search(self, query: str, *, page_cursor: str | None) -> HistoryPage:
        return self._query("search", query, page_cursor)

    def resolve_reference(
        self, reference_id: str, *, page_cursor: str | None
    ) -> HistoryPage:
        return self._query("resolve_reference", reference_id, page_cursor)

    def list_open_commitments(self, *, page_cursor: str | None) -> HistoryPage:
        return self._query("list_open_commitments", None, page_cursor)

    def get_current_state(self, *, page_cursor: str | None) -> HistoryPage:
        return self._query("get_current_state", None, page_cursor)
