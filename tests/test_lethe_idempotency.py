from __future__ import annotations

from datetime import UTC, datetime

import httpx

from vsm.kernel.models import EventEnvelope
from vsm.lethe.client import LetheOperationalLedger


def test_operational_retry_sends_the_identical_lethe_event(
    monkeypatch,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(httpx.Response(200, content=request.content).json())
        outcome = "appended" if len(requests) == 1 else "duplicate"
        return httpx.Response(
            200,
            json={
                "outcomes": [
                    {
                        "outcome": outcome,
                        "cursor": 1,
                        "stream_version": 1,
                    }
                ]
            },
        )

    real_client = httpx.Client

    def client_factory(**kwargs):
        return real_client(
            base_url=kwargs["base_url"],
            headers=kwargs["headers"],
            timeout=kwargs["timeout"],
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("vsm.lethe.client.httpx.Client", client_factory)
    ledger = LetheOperationalLedger(
        base_url="https://lethe.test",
        bearer_token="test-secret",
        data_space_id="data-space:personal",
        timeout_seconds=5,
        max_page_size=100,
    )
    event = EventEnvelope(
        event_id="event:stable",
        data_space_id="data-space:personal",
        stream_id="stream:stable",
        stream_version=1,
        event_type="test_event",
        occurred_at=datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
        actor_type="system",
        actor_id="system:test",
        correlation_id=None,
        causation_id=None,
        idempotency_key="test:stable",
        payload={"value": 1},
    )
    try:
        first = ledger.append(event, 0)
        second = ledger.append(event, 0)
    finally:
        ledger.close()

    assert first.outcome == "appended"
    assert second.outcome == "duplicate"
    assert requests[0] == requests[1]
    observation = requests[0]["requests"][0]["event"]["observation"]
    assert observation["published"] == "2026-07-20T00:00:00Z"
    assert observation["recorded_at"] == "2026-07-20T00:00:00Z"
