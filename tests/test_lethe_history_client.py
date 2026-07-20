import json

import httpx
import pytest

from vsm.lethe.client import LetheHistoryClient, LetheOperationalLedger


@pytest.mark.parametrize(
    ("invoke", "operation", "argument", "page_cursor"),
    (
        (lambda client: client.list_sessions(page_cursor=None), "list_sessions", {}, None),
        (
            lambda client: client.read_timeline("history-session:one", page_cursor="cursor:one"),
            "read_timeline",
            {"session_id": "history-session:one"},
            "cursor:one",
        ),
        (
            lambda client: client.read_raw("message:one", page_cursor=None),
            "read_raw",
            {"message_id": "message:one"},
            None,
        ),
        (
            lambda client: client.search("Interface Pilot", page_cursor=None),
            "search",
            {"query": "Interface Pilot"},
            None,
        ),
        (
            lambda client: client.resolve_reference("reference:one", page_cursor=None),
            "resolve_reference",
            {"reference_id": "reference:one"},
            None,
        ),
        (
            lambda client: client.list_open_commitments(page_cursor=None),
            "list_open_commitments",
            {},
            None,
        ),
        (
            lambda client: client.get_current_state(state_key=None, page_cursor=None),
            "get_current_state",
            {},
            None,
        ),
        (
            lambda client: client.get_current_state(state_key="quota", page_cursor=None),
            "get_current_state",
            {"state_key": "quota"},
            None,
        ),
    ),
)
def test_history_query_uses_lethe_typed_argument_contract(
    invoke, operation, argument, page_cursor
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/history/query"
        assert request.headers["Authorization"] == "Bearer history-token"
        assert json.loads(request.content) == {
            "data_space_id": "space:personal",
            "operation": operation,
            "argument": argument,
            "page_cursor": page_cursor,
            "max_result_bytes": 1024,
        }
        return httpx.Response(
            200,
            json={
                "result_json": {"operation": operation},
                "next_cursor": None,
                "source_cursor": "operational:7",
            },
        )

    client = LetheHistoryClient(
        base_url="https://lethe.test",
        bearer_token="history-token",
        data_space_id="space:personal",
        timeout_seconds=1,
        max_result_bytes=1024,
    )
    client._client = httpx.Client(
        base_url="https://lethe.test",
        headers={"Authorization": "Bearer history-token"},
        transport=httpx.MockTransport(handler),
    )
    try:
        response = invoke(client)
    finally:
        client.close()

    assert response.result_json == {"operation": operation}


def test_operational_adapter_marks_only_lethe_history_events_as_external():
    raw_event_id = f"event:history-message:{'a' * 64}"
    stored = LetheOperationalLedger._stored(
        {
            "cursor": 7,
            "event": {
                "event_id": raw_event_id,
                "data_space_id": "space:personal",
                "stream_id": f"history-message:{'b' * 64}",
                "stream_version": 1,
                "event_type": "history.message_imported",
                "occurred_at": "2026-07-20T00:00:00Z",
                "actor_type": "history_source",
                "actor_id": "assistant",
                "correlation_id": None,
                "causation_id": None,
                "observation": {
                    "idempotency_key": "history-native-id",
                    "payload": {"raw_blob_ref": f"blob:sha256:{'c' * 64}"},
                },
            },
        }
    )

    assert stored.cursor == 7
    assert stored.event.event_id.startswith("event:")
    assert stored.event.event_id != raw_event_id
    assert stored.event.stream_id == f"history-message:{'b' * 64}"
    assert stored.event.event_type == "history.message_imported"
    assert stored.event.actor_type == "system"
    assert stored.event.actor_id is None
    assert stored.event.payload == {}
