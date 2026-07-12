"""Unit tests for ``vsm status`` readable output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vsm.cli import app


runner = CliRunner()


def _write_events_file(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")


def test_status_empty_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-empty"
    (tmp_path / "runs" / run_id).mkdir(parents=True)
    (tmp_path / "runs" / run_id / "events.jsonl").touch()

    result = runner.invoke(app, ["status", run_id])

    assert result.exit_code == 0
    assert "Run: run-empty" in result.stdout
    assert "Events: 0" in result.stdout
    assert "Tasks:" in result.stdout
    assert "Systems:" in result.stdout
    assert "none" in result.stdout


def test_status_with_one_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-task"
    events_path = tmp_path / "runs" / run_id / "events.jsonl"
    _write_events_file(
        events_path,
        [
            {
                "ts": "2025-01-01T00:00:00.000Z",
                "run_id": run_id,
                "event_type": "task_submitted",
                "seq": 0,
                "payload": {
                    "task_id": "task-abc",
                    "run_id": run_id,
                    "description": "日本語で要約してください",
                    "file_paths": [],
                    "submitted_at": "2025-01-01T00:00:00.000Z",
                },
            },
        ],
    )

    result = runner.invoke(app, ["status", run_id])

    assert result.exit_code == 0
    assert "task task-abc" in result.stdout
    assert "state: submitted" in result.stdout
    assert "description: 日本語で要約してください" in result.stdout


def test_status_derives_completed_state_from_s1_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-done"
    events_path = tmp_path / "runs" / run_id / "events.jsonl"
    _write_events_file(
        events_path,
        [
            {
                "ts": "2025-01-01T00:00:00.000Z",
                "run_id": run_id,
                "event_type": "task_submitted",
                "seq": 0,
                "payload": {
                    "task_id": "task-abc",
                    "run_id": run_id,
                    "description": "write code",
                    "file_paths": [],
                    "submitted_at": "2025-01-01T00:00:00.000Z",
                },
            },
            {
                "ts": "2025-01-01T00:00:00.010Z",
                "run_id": run_id,
                "event_type": "s1_completion",
                "seq": 1,
                "payload": {
                    "s1_id": "s1-1",
                    "work_item_id": "work-1",
                    "result": {"success": True, "text": "done"},
                },
            },
        ],
    )

    result = runner.invoke(app, ["status", run_id])

    assert result.exit_code == 0
    assert "state: completed (from s1_completion)" in result.stdout


def test_status_with_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-sys"
    events_path = tmp_path / "runs" / run_id / "events.jsonl"
    _write_events_file(
        events_path,
        [
            {
                "ts": "2025-01-01T00:00:00.000Z",
                "run_id": run_id,
                "event_type": "system_instantiated",
                "seq": 0,
                "payload": {
                    "system_id": "sys-1",
                    "role": "S5_POLICY",
                    "sub_agent_count": 3,
                },
            },
            {
                "ts": "2025-01-01T00:00:00.001Z",
                "run_id": run_id,
                "event_type": "system_instantiated",
                "seq": 1,
                "payload": {
                    "system_id": "sys-2",
                    "role": "S4_SCANNER",
                    "sub_agent_count": 2,
                },
            },
        ],
    )

    result = runner.invoke(app, ["status", run_id])

    assert result.exit_code == 0
    assert "S5_POLICY" in result.stdout
    assert "S4_SCANNER" in result.stdout
    assert "Sub_Agents: 3" in result.stdout
    assert "Sub_Agents: 2" in result.stdout


def test_status_shows_budget_consumption_by_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-budget"
    events_path = tmp_path / "runs" / run_id / "events.jsonl"
    _write_events_file(
        events_path,
        [
            {
                "ts": "2025-01-01T00:00:00.000Z",
                "run_id": run_id,
                "event_type": "system_instantiated",
                "seq": 0,
                "payload": {"system_id": "node-1", "role": "S1_WORKER", "sub_agent_count": 1},
            },
            {
                "ts": "2025-01-01T00:00:00.001Z",
                "run_id": run_id,
                "event_type": "budget_consumed",
                "seq": 1,
                "payload": {
                    "node_id": "node-1",
                    "tokens_in": 10,
                    "tokens_out": 4,
                    "tokens_cache_read": 2,
                    "wall_clock_ms": 1250,
                },
            },
        ],
    )

    result = runner.invoke(app, ["status", run_id])

    assert result.exit_code == 0
    assert "Budget consumption by Node:" in result.stdout
    assert "tokens: 16 (in 10 / out 4 / cache 2)" in result.stdout
    assert "wall: 1.250s" in result.stdout


def test_status_missing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "run-missing"])

    assert result.exit_code == 2
    assert "No events found for run run-missing" in result.stderr
    assert "vsm runs" in result.stderr


def test_status_invalid_run_id() -> None:
    result = runner.invoke(app, ["status", ""])

    assert result.exit_code != 0
