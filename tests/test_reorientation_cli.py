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


def test_reorientation_start_resolves_owner_without_internal_ids(monkeypatch) -> None:
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
                json={
                    "state": "REORIENTATION_ONLY",
                    "assessment": None,
                    "reorientation_error": None,
                    "import_receipt": {
                        "sessions": [
                            {"session_ref": f"history-session:{index}"}
                            for index in range(832)
                        ]
                    },
                },
            )
        raise AssertionError(request.url)

    _install_transport(monkeypatch, handler)
    result = CliRunner().invoke(app, ["reorientation", "start", *_base_arguments()])

    assert result.exit_code == 0, result.output
    assert requests[-1] == (
        "POST",
        "/api/reorientation/start",
        {
            "actor_id": "human:owner",
            "idempotency_key": "owner:explicit-command",
        },
    )
    output = json.loads(result.output)
    assert output == {
        "state": "REORIENTATION_ONLY",
        "assessment_ready": False,
        "reorientation_error": None,
    }
    assert "history-session:" not in result.output


def test_reorientation_approve_uses_current_assessment_and_records_corrections(
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
                        "resume_work_item_ids": ["work:current"],
                    },
                },
            )
        if request.url.path == "/api/data-spaces":
            return httpx.Response(200, json=[{"owner_id": "human:owner"}])
        if request.url.path == "/api/reorientation/approval":
            approval.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "state": "ACTIVE",
                    "assessment": {
                        "assessment_id": "assessment:current",
                        "conversation_id": "conversation:owner-main",
                        "resume_work_item_ids": ["work:current"],
                    },
                    "reorientation_error": None,
                },
            )
        raise AssertionError(request.url)

    _install_transport(monkeypatch, handler)
    result = CliRunner().invoke(
        app,
        [
            "reorientation",
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
    assert json.loads(result.output) == {
        "state": "ACTIVE",
        "assessment_ready": True,
        "reorientation_error": None,
    }


def test_reorientation_approve_fails_before_owner_confirmation(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/activation/status"
        return httpx.Response(
            200,
            json={"state": "REORIENTATION_ONLY", "assessment": None},
        )

    _install_transport(monkeypatch, handler)
    result = CliRunner().invoke(app, ["reorientation", "approve", *_base_arguments()])

    assert result.exit_code != 0
    assert "no owner-confirmable assessment" in str(result.exception)


def test_reorientation_approve_rejects_empty_resume_work(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/activation/status"
        return httpx.Response(
            200,
            json={
                "state": "AWAITING_OWNER_CONFIRMATION",
                "assessment": {
                    "assessment_id": "assessment:incomplete",
                    "conversation_id": "conversation:owner-main",
                    "resume_work_item_ids": [],
                },
            },
        )

    _install_transport(monkeypatch, handler)
    result = CliRunner().invoke(app, ["reorientation", "approve", *_base_arguments()])

    assert result.exit_code != 0
    assert "no real resume WorkItem" in str(result.exception)


def test_reorientation_revise_resolves_owner_and_uses_explicit_reason(
    monkeypatch,
) -> None:
    requests: list[tuple[str, str, object | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/data-spaces":
            return httpx.Response(200, json=[{"owner_id": "human:owner"}])
        if request.url.path == "/api/reorientation/revision":
            return httpx.Response(
                200,
                json={
                    "state": "REORIENTATION_ONLY",
                    "assessment": None,
                    "reorientation_error": None,
                },
            )
        raise AssertionError(request.url)

    _install_transport(monkeypatch, handler)
    result = CliRunner().invoke(
        app,
        [
            "reorientation",
            "revise",
            *_base_arguments(),
            "--reason",
            "missing_resume_work_item",
        ],
    )

    assert result.exit_code == 0, result.output
    assert requests[-1] == (
        "POST",
        "/api/reorientation/revision",
        {
            "reason_code": "missing_resume_work_item",
            "requested_by": "owner",
            "actor_id": "human:owner",
            "idempotency_key": "owner:explicit-command",
        },
    )
    assert json.loads(result.output) == {
        "state": "REORIENTATION_ONLY",
        "assessment_ready": False,
        "reorientation_error": None,
    }
