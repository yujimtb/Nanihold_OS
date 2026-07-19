from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from tools.capture_system_snapshot import capture_snapshot, write_snapshot
from tools.history_source_export import HistorySourceExportError


def spec(tmp_path: Path) -> dict[str, object]:
    repository = tmp_path / "repository"
    repository.mkdir()
    config = tmp_path / "config.toml"
    config.write_text("explicit = true\n", "utf-8")
    return {
        "captured_at": "2026-07-20T00:00:00Z",
        "source_instance_id": "desktop-primary",
        "repositories": [{"id": "nanihold", "path": str(repository)}],
        "endpoints": [
            {
                "state_key": "nanihold:activation",
                "url": "https://nanihold.example.invalid/api/activation/status",
                "bearer_token_env": "TEST_NANIHOLD_TOKEN",
                "device_id": "device:owner",
                "selected_fields": ["state", "history_cursor"],
                "timeout_seconds": 5,
            }
        ],
        "fingerprinted_files": [
            {"state_key": "config:nanihold", "path": str(config)}
        ],
    }


def test_capture_is_explicit_bounded_and_does_not_store_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = spec(tmp_path)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(document), "utf-8")
    monkeypatch.setenv("TEST_NANIHOLD_TOKEN", "never-persist-this")

    def fake_run(argv, **kwargs):
        assert kwargs["shell"] is False
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, "a" * 40 + "\n", "")
        if argv[-2:] == ["branch", "--show-current"]:
            return subprocess.CompletedProcess(argv, 0, "feature/full\n", "")
        if argv[-3:] == ["status", "--porcelain=v1", "-z"]:
            return subprocess.CompletedProcess(argv, 0, " M docs/a.md\0", "")
        raise AssertionError(argv)

    monkeypatch.setattr("tools.capture_system_snapshot.subprocess.run", fake_run)

    def fake_get(url, *, headers, timeout):
        assert headers["Authorization"] == "Bearer never-persist-this"
        assert headers["X-Nanihold-Device-Id"] == "device:owner"
        assert timeout == 5.0
        return httpx.Response(
            200,
            json={
                "state": "HISTORY_IMPORTED",
                "history_cursor": 42,
                "unselected_private_detail": "not persisted",
            },
        )

    snapshot = capture_snapshot(spec_path.resolve(), request=fake_get)
    serialized = json.dumps(snapshot)
    assert "never-persist-this" not in serialized
    assert "unselected_private_detail" not in serialized
    assert [state["state_key"] for state in snapshot["states"]] == [
        "git:nanihold",
        "nanihold:activation",
        "config:nanihold",
    ]
    assert snapshot["states"][0]["value"]["status_entries"] == (" M docs/a.md",)

    output = tmp_path / "output" / "snapshot.json"
    write_snapshot(snapshot, output)
    assert json.loads(output.read_text("utf-8"))["source_instance_id"] == "desktop-primary"
    with pytest.raises(HistorySourceExportError, match="must not already exist"):
        write_snapshot(snapshot, output)


def test_capture_fails_when_required_credential_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = spec(tmp_path)
    document["repositories"] = []
    document["fingerprinted_files"] = []
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(document), "utf-8")
    monkeypatch.delenv("TEST_NANIHOLD_TOKEN", raising=False)
    with pytest.raises(HistorySourceExportError, match="credential"):
        capture_snapshot(spec_path.resolve())


def test_capture_rejects_extra_spec_fields(tmp_path: Path) -> None:
    document = spec(tmp_path)
    document["fallback_url"] = "http://localhost"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(document), "utf-8")
    with pytest.raises(HistorySourceExportError, match="requires exactly"):
        capture_snapshot(spec_path.resolve())


def test_capture_rejects_credentials_in_endpoint_url(tmp_path: Path) -> None:
    document = spec(tmp_path)
    document["repositories"] = []
    document["fingerprinted_files"] = []
    document["endpoints"][0]["url"] = "https://token@example.invalid/health"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(document), "utf-8")
    with pytest.raises(HistorySourceExportError, match="without credentials"):
        capture_snapshot(spec_path.resolve())
