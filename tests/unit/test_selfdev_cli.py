"""Wave 4 selfdev CLI transport tests."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vsm.cli import app


class _Response:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def test_selfdev_list_uses_loopback_api_and_canonical_json(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Response({"items": [{"proposal_id": "proposal-a", "state": "MERGE_READY"}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = CliRunner().invoke(app, ["selfdev", "list", "--state", "MERGE_READY", "--json"])
    assert result.exit_code == 0
    assert str(captured["url"]).startswith("http://127.0.0.1:8000/api/selfdev/proposals?")
    assert json.loads(result.stdout)["items"][0]["state"] == "MERGE_READY"


def test_selfdev_propose_reads_request_file_without_direct_store_fallback(monkeypatch, tmp_path: Path) -> None:
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(
        json.dumps(
            {
                "title": "CLI proposal",
                "motivation": "transport を検証する",
                "scope": [{"path": "docs/api.md", "kind": "file"}],
                "acceptance_criteria": [{
                    "id": "AC-1", "statement": "存在する",
                    "verifier": {"kind": "path_exists", "path": "docs/api.md"},
                }],
                "risk_class": "normal",
                "budget_estimate": {"tokens": 10, "active_wall_clock_seconds": 5, "pool_quota": []},
                "origin": {"kind": "conversation", "decision_ref": "cli", "conversation_id": "chat-cli"},
                "dependencies": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        return _Response({"proposal_id": "proposal-b", "state": "PROPOSED", "state_version": 1})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = CliRunner().invoke(app, ["selfdev", "propose", "--file", str(proposal_file)])
    assert result.exit_code == 0
    assert str(captured["url"]).endswith("/api/selfdev/proposals")
    assert "id" not in captured["payload"]
    assert json.loads(result.stdout)["state"] == "PROPOSED"


def test_selfdev_approve_posts_expected_version(monkeypatch) -> None:
    captured: dict[str, object] = {}
    calls: list[str] = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if request.full_url.endswith("/proposals/proposal-a"):
            return _Response({"proposal_manifest_sha256": "a" * 64, "protected_scope_sha256": "b" * 64})
        captured["payload"] = json.loads(request.data)
        return _Response({"accepted": True, "decision": "approve"})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = CliRunner().invoke(
        app,
        ["selfdev", "approve", "proposal-a", "--reason", "事前承認", "--state-version", "8"],
    )
    assert result.exit_code == 0
    assert calls[0].endswith("/proposals/proposal-a")
    assert captured["payload"] == {
        "decision": "approve",
        "reason": "事前承認",
        "statement": None,
        "expected_state_version": 8,
        "proposal_manifest_sha256": "a" * 64,
        "protected_scope_sha256": "b" * 64,
    }
