from __future__ import annotations

import httpx
import pytest

from vsm.errors import ModelMismatch
from vsm.interface.pilot_host import PilotHostInterfacePilot
from vsm.pilot.models import (
    EventDeltaSummary,
    InterfaceResumePack,
    InterfaceTurn,
    ModelCandidate,
)


def candidate() -> ModelCandidate:
    return ModelCandidate(
        adapter="claude-code",
        adapter_version="2.1.215",
        provider="anthropic",
        model_snapshot="claude-haiku-4-5-20251001",
        effort="low",
        toolset=("conversation-only",),
        sandbox_fingerprint="observe-only:no-tools",
        environment_fingerprint="sha256:test",
    )


def turn() -> InterfaceTurn:
    return InterfaceTurn(
        owner_message_blob_ref=f"blob:sha256:{'a' * 64}",
        event_delta=EventDeltaSummary(
            after_cursor=0,
            through_cursor=0,
            event_count=0,
            event_type_counts={},
            changed_stream_ids=(),
        ),
        resume_pack=InterfaceResumePack(
            node_memory=(),
            unfinished_work_items=(),
            open_commitments=(),
            active_decisions=(),
        ),
        provider_session_id=None,
    )


class FakeClient:
    actual_model = "claude-haiku-4-5-20251001"
    instances: list["FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.posts: list[dict[str, object]] = []
        self.__class__.instances.append(self)

    def get(self, path: str) -> httpx.Response:
        assert path == "/health"
        return httpx.Response(
            200,
            json={"candidate_key": candidate().key},
        )

    def post(self, path: str, *, json: dict[str, object]) -> httpx.Response:
        assert path == "/v1/interface-turn"
        self.posts.append(json)
        return httpx.Response(
            200,
            json={
                "requested_candidate_key": candidate().key,
                "actual_provider": "anthropic",
                "actual_model_snapshot": self.actual_model,
                "structured_response": {
                    "display_text": "確認できました",
                    "actions": [],
                    "provider_session_id": "provider-session",
                    "pilot_usage": {
                        "candidate_key": candidate().key,
                        "actual_provider": "anthropic",
                        "actual_model_snapshot": self.actual_model,
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 20,
                        "cache_read_input_tokens": 30,
                        "output_tokens": 5,
                        "cost_usd": 0.001,
                        "duration_ms": 100,
                        "classifier_triggered": False,
                        "model_substitution": False,
                        "full_history_resent": False,
                        "polling_call": False,
                        "false_complete": False,
                        "reedited_tokens": 0,
                    },
                },
            },
        )

    def close(self) -> None:
        self.closed = True


def test_pilot_host_is_authenticated_and_returns_one_structured_response(
    monkeypatch,
):
    FakeClient.instances.clear()
    FakeClient.actual_model = candidate().model_snapshot
    monkeypatch.setattr("vsm.interface.pilot_host.httpx.Client", FakeClient)
    pilot = PilotHostInterfacePilot(
        candidate=candidate(),
        base_url="http://pilot-proxy:8765/",
        bearer_token="secret",
        timeout_seconds=120,
    )

    result = pilot.respond(owner_text="確認", context=turn())

    client = FakeClient.instances[-1]
    assert client.kwargs["base_url"] == "http://pilot-proxy:8765"
    assert client.kwargs["headers"] == {"Authorization": "Bearer secret"}
    assert len(client.posts) == 1
    assert "key" not in client.posts[0]["candidate"]
    assert result.display_text == "確認できました"
    assert result.pilot_usage.input_tokens == 10
    pilot.close()
    assert client.closed


def test_pilot_host_rejects_actual_model_substitution(monkeypatch):
    FakeClient.instances.clear()
    FakeClient.actual_model = "claude-opus-4-1"
    monkeypatch.setattr("vsm.interface.pilot_host.httpx.Client", FakeClient)
    pilot = PilotHostInterfacePilot(
        candidate=candidate(),
        base_url="http://pilot-proxy:8765",
        bearer_token="secret",
        timeout_seconds=120,
    )

    with pytest.raises(ModelMismatch, match="RequestedActualModelMismatch"):
        pilot.respond(owner_text="確認", context=turn())
