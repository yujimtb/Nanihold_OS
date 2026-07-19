from __future__ import annotations

import httpx

from vsm.lethe.client import LetheOperationalLedger


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests: list[tuple[str, dict[str, int]]] = []
        self.__class__.instances.append(self)

    def get(self, path: str, *, params: dict[str, int]) -> httpx.Response:
        self.requests.append((path, params))
        return httpx.Response(200, json={"events": []})

    def close(self) -> None:
        return None


def test_lethe_page_and_stream_are_clamped_to_configured_contract(monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr("vsm.lethe.client.httpx.Client", FakeClient)
    ledger = LetheOperationalLedger(
        base_url="http://lethe:8080/",
        bearer_token="secret",
        data_space_id="space:local",
        timeout_seconds=30,
        max_page_size=25,
    )

    assert ledger.page(7, 1000) == []
    assert ledger.stream("conversation:local", 3, 1000) == []

    assert FakeClient.instances[-1].requests == [
        (
            "/api/operational-events",
            {"after_cursor": 7, "limit": 25},
        ),
        (
            "/api/operational-streams/conversation:local",
            {"after_stream_version": 3, "limit": 25},
        ),
    ]
