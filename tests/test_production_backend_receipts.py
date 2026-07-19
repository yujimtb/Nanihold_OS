from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from vsm.pilot.models import (
    DeviceIdentity,
    EventDeltaSummary,
    InterfaceTurn,
    ModelCandidate,
    PilotMode,
)
from vsm.pilot.production_host import (
    PilotHostTransportUnknown,
    ProductionPilotHostClient,
    WorkExecutionProfile,
)


IDENTITY = DeviceIdentity(
    pilot_host_id="pilot-host:production",
    device_id="device:production",
    certificate_sha256="a" * 64,
)


def candidate(adapter: str, provider: str, model: str, effort: str) -> ModelCandidate:
    return ModelCandidate(
        adapter=adapter,
        adapter_version="1",
        provider=provider,
        model_snapshot=model,
        effort=effort,
        toolset=("typed-tool",),
        sandbox_fingerprint="sandbox:test",
        environment_fingerprint="environment:test",
    )


INTERFACE = candidate("claude-code", "anthropic", "claude-fable-5", "high")
CODING = candidate("codex-cli", "openai", "gpt-5.6-sol", "xhigh")


def turn(provider_session_id: str) -> InterfaceTurn:
    return InterfaceTurn(
        owner_message_blob_ref=f"blob:sha256:{'b' * 64}",
        event_delta=EventDeltaSummary(
            after_cursor=0,
            through_cursor=0,
            event_count=0,
            event_type_counts={},
            changed_stream_ids=(),
        ),
        resume_pack=None,
        provider_session_id=provider_session_id,
    )


class ReceiptClient:
    instances: list["ReceiptClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.posts: list[dict[str, object]] = []
        self.__class__.instances.append(self)

    def get(self, path: str):
        assert path == "/health"
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "identity": IDENTITY.model_dump(mode="json"),
                "candidates": {
                    "interface": {
                        "candidate_key": INTERFACE.key,
                        "model_snapshot": INTERFACE.model_snapshot,
                        "effort": INTERFACE.effort,
                    },
                    "coding_s1": {
                        "candidate_key": CODING.key,
                        "model_snapshot": CODING.model_snapshot,
                        "effort": CODING.effort,
                    },
                },
                "permission_mode": "sandboxed_bypass",
                "receipt_reconciliation": True,
            },
        )

    def post(self, path: str, *, json: dict[str, object]):
        self.posts.append(json)
        leaf = f"provider-leaf-{len(self.posts)}"
        digest = hashlib.sha256(
            __import__("json").dumps(
                json, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode()
        ).hexdigest()
        return httpx.Response(
            200,
            json={
                "receipt_id": json["receipt_id"],
                "endpoint": path,
                "idempotency_key": json["idempotency_key"],
                "request_sha256": digest,
                "status": "succeeded",
                "candidate_key": INTERFACE.key,
                "requested_model": INTERFACE.model_snapshot,
                "actual_model": INTERFACE.model_snapshot,
                "provider_session_id": leaf,
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 5,
                    "output_tokens": 2,
                    "cost_usd": 0.01,
                    "duration_ms": 5,
                    "classifier_triggered": False,
                    "permission_rejections": 0,
                    "permission_mode": "sandboxed_bypass",
                    "model_substitution": False,
                },
                "result": {"display_text": "ok", "actions": []},
                "error": None,
                "created_at": "2026-07-20T00:00:00Z",
                "updated_at": "2026-07-20T00:00:00Z",
            },
        )

    def close(self):
        return None


def make_client(monkeypatch) -> ProductionPilotHostClient:
    ReceiptClient.instances.clear()
    monkeypatch.setattr("vsm.pilot.production_host.httpx.Client", ReceiptClient)
    return ProductionPilotHostClient(
        base_url="https://pilot-host.test",
        bearer_token="secret",
        identity=IDENTITY,
        interface_candidate=INTERFACE,
        coding_candidate=CODING,
        permission_mode=PilotMode.SANDBOXED_BYPASS,
        interface_max_budget_usd=1,
        interface_timeout_seconds=30,
        work_profile=WorkExecutionProfile(
            cwd="/workspace",
            sandbox="workspace-write",
            max_input_tokens=100,
            max_output_tokens=50,
            max_total_tokens=150,
            timeout_seconds=30,
        ),
        transport_timeout_seconds=60,
    )


def test_interface_session_leaf_advances_a_to_b_to_c(monkeypatch):
    client = make_client(monkeypatch)
    first = client.respond(owner_text="one", context=turn("provider-root-a"))
    second = client.respond(
        owner_text="two", context=turn(first.provider_session_id)
    )

    assert first.provider_session_id == "provider-leaf-1"
    assert second.provider_session_id == "provider-leaf-2"
    posts = ReceiptClient.instances[-1].posts
    assert [item["root_session_id"] for item in posts] == [
        "provider-root-a",
        "provider-leaf-1",
    ]
    assert all(item["fork_session"] is True for item in posts)
    assert ReceiptClient.instances[-1].kwargs["headers"] == {
        "Authorization": "Bearer secret",
        "X-Nanihold-Pilot-Host-Id": "pilot-host:production",
        "X-Nanihold-Device-Id": "device:production",
        "X-Nanihold-Device-Certificate-Sha256": "a" * 64,
    }


def test_transport_error_reconciles_by_get_and_never_reposts(monkeypatch):
    class UnknownClient(ReceiptClient):
        def post(self, path, *, json):
            self.posts.append(json)
            raise httpx.ReadTimeout(
                "unknown", request=httpx.Request("POST", f"https://host{path}")
            )

        def get(self, path):
            if path == "/health":
                return super().get(path)
            payload = self.posts[0]
            digest = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            return httpx.Response(
                200,
                json={
                    "receipt_id": payload["receipt_id"],
                    "endpoint": "/v1/interface-turn",
                    "idempotency_key": payload["idempotency_key"],
                    "request_sha256": digest,
                    "status": "transport_unknown",
                    "candidate_key": INTERFACE.key,
                    "requested_model": INTERFACE.model_snapshot,
                    "actual_model": None,
                    "provider_session_id": None,
                    "usage": None,
                    "result": None,
                    "error": {
                        "code": "TransportUnknown",
                        "message": "reconciliation required",
                    },
                    "created_at": "2026-07-20T00:00:00Z",
                    "updated_at": "2026-07-20T00:00:00Z",
                },
            )

    monkeypatch.setattr("vsm.pilot.production_host.httpx.Client", UnknownClient)
    client = ProductionPilotHostClient(
        base_url="https://pilot-host.test",
        bearer_token="secret",
        identity=IDENTITY,
        interface_candidate=INTERFACE,
        coding_candidate=CODING,
        permission_mode=PilotMode.SANDBOXED_BYPASS,
        interface_max_budget_usd=1,
        interface_timeout_seconds=30,
        work_profile=WorkExecutionProfile(
            cwd="/workspace",
            sandbox="workspace-write",
            max_input_tokens=100,
            max_output_tokens=50,
            max_total_tokens=150,
            timeout_seconds=30,
        ),
        transport_timeout_seconds=60,
    )
    with pytest.raises(PilotHostTransportUnknown):
        client.respond(owner_text="one", context=turn("provider-root-a"))
    assert len(client._client.posts) == 1
