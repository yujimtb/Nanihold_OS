from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx

from vsm.errors import InvariantViolation, StreamConflict
from vsm.kernel.models import AppendResult, EventEnvelope, StoredEvent


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
        observation_id = str(uuid.uuid4())
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
                    "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
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
        response = self._client.post(
            "/api/operational-events", json={"requests": [request]}
        )
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
