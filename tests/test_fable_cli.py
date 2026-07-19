from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner

from vsm.cli import app


def _install_transport(monkeypatch, handler) -> None:
    real_client = httpx.Client

    def client_factory(**kwargs):
        return real_client(
            base_url=kwargs["base_url"],
            headers=kwargs["headers"],
            timeout=kwargs["timeout"],
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("vsm.cli.httpx.Client", client_factory)
    monkeypatch.setenv("TEST_OWNER_TOKEN", "secret-from-env")


def _base_arguments() -> list[str]:
    return [
        "--base-url",
        "https://nanihold.test",
        "--bearer-token-env",
        "TEST_OWNER_TOKEN",
        "--device-id",
        "device:owner-terminal",
        "--idempotency-key",
        "owner:explicit-command",
    ]


def test_fable_catch_up_resolves_owner_without_internal_ids(monkeypatch) -> None:
    requests: list[tuple[str, str, object | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer secret-from-env"
        assert (
            request.headers["x-nanihold-device-id"]
            == "device:owner-terminal"
        )
        if request.url.path == "/api/data-spaces":
            return httpx.Response(
                200,
                json=[{"owner_id": "human:owner"}],
            )
        if request.url.path == "/api/reorientation/start":
            return httpx.Response(
                202,
                json={"state": "REORIENTATION_ONLY"},
            )
        raise AssertionError(request.url)

    _install_transport(monkeypatch, handler)
    result = CliRunner().invoke(app, ["fable", "catch-up", *_base_arguments()])

    assert result.exit_code == 0, result.output
    assert requests[-1] == (
        "POST",
        "/api/reorientation/start",
        {
            "actor_id": "human:owner",
            "idempotency_key": "owner:explicit-command",
        },
    )


def test_fable_approve_uses_current_assessment_and_records_corrections(
    monkeypatch,
) -> None:
    approval: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/activation/status":
            return httpx.Response(
                200,
                json={
                    "state": "AWAITING_OWNER_CONFIRMATION",
                    "assessment": {
                        "assessment_id": "assessment:current",
                        "conversation_id": "conversation:owner-main",
                    },
                },
            )
        if request.url.path == "/api/data-spaces":
            return httpx.Response(200, json=[{"owner_id": "human:owner"}])
        if request.url.path == "/api/reorientation/approval":
            approval.update(json.loads(request.content))
            return httpx.Response(200, json={"state": "ACTIVE"})
        raise AssertionError(request.url)

    _install_transport(monkeypatch, handler)
    result = CliRunner().invoke(
        app,
        [
            "fable",
            "approve",
            *_base_arguments(),
            "--correction",
            "Bを先にする",
            "--correction",
            "約束Cは完了済み",
        ],
    )

    assert result.exit_code == 0, result.output
    assert approval == {
        "assessment_id": "assessment:current",
        "conversation_id": "conversation:owner-main",
        "corrections": ["Bを先にする", "約束Cは完了済み"],
        "actor_id": "human:owner",
        "idempotency_key": "owner:explicit-command",
    }


def test_fable_approve_fails_before_owner_confirmation(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/activation/status"
        return httpx.Response(
            200,
            json={"state": "REORIENTATION_ONLY", "assessment": None},
        )

    _install_transport(monkeypatch, handler)
    result = CliRunner().invoke(app, ["fable", "approve", *_base_arguments()])

    assert result.exit_code != 0
    assert "no owner-confirmable assessment" in str(result.exception)
