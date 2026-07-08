"""Unit tests for ``vsm replay`` readable and raw output."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vsm.cli import app


runner = CliRunner()


def _write_events_file(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")


def test_replay_raw_format_single_event(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-replay"
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
                    "role": "S1_WORKER",
                    "sub_agent_count": 1,
                },
            },
        ],
    )

    result = runner.invoke(app, ["replay", run_id, "--raw"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == (
        "2025-01-01T00:00:00.000Z sys-1 - system_instantiated"
    )


def test_replay_summarises_selected_payloads(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-summary"
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
                    "task_id": "task-1",
                    "run_id": run_id,
                    "description": "日本語の計画を作る",
                    "file_paths": ["a.txt"],
                    "submitted_at": "2025-01-01T00:00:00.000Z",
                },
            },
            {
                "ts": "2025-01-01T00:00:00.001Z",
                "run_id": run_id,
                "event_type": "policy_decision",
                "seq": 1,
                "payload": {
                    "decision_id": "d1",
                    "assessment_id": "a1",
                    "directive": "go",
                    "followup_request": "scan",
                },
            },
            {
                "ts": "2025-01-01T00:00:00.002Z",
                "run_id": run_id,
                "event_type": "tool_completed",
                "seq": 2,
                "payload": {
                    "tool_invocation_id": "tool-1",
                    "tool_name": "codex_run",
                    "result": {"ok": True},
                },
            },
        ],
    )

    result = runner.invoke(app, ["replay", run_id])

    assert result.exit_code == 0, result.stderr
    assert "task: 日本語の計画を作る (files=1)" in result.stdout
    assert "decision: go" in result.stdout
    assert "tool: codex_run completed" in result.stdout


def test_replay_channel_message_raw_format(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-ch"
    events_path = tmp_path / "runs" / run_id / "events.jsonl"
    _write_events_file(
        events_path,
        [
            {
                "ts": "2025-01-01T00:00:00.000Z",
                "run_id": run_id,
                "event_type": "channel_message",
                "seq": 0,
                "payload": {
                    "sender": "s4-id",
                    "receiver": "s5-id",
                    "channel": "S4-S5",
                    "payload": {"k": 1},
                },
            },
        ],
    )

    result = runner.invoke(app, ["replay", run_id, "--raw"])

    assert result.exit_code == 0, result.stderr
    assert "s4-id" in result.stdout
    assert "S4-S5" in result.stdout
    assert "channel_message" in result.stdout


def test_replay_raw_preserves_order(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-order"
    events_path = tmp_path / "runs" / run_id / "events.jsonl"
    _write_events_file(
        events_path,
        [
            {
                "ts": f"2025-01-01T00:00:00.{i:03d}Z",
                "run_id": run_id,
                "event_type": "system_instantiated",
                "seq": i,
                "payload": {
                    "system_id": f"sys-{i}",
                    "role": "S1_WORKER",
                    "sub_agent_count": 1,
                },
            }
            for i in range(5)
        ],
    )

    result = runner.invoke(app, ["replay", run_id, "--raw"])

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 5
    for i, line in enumerate(lines):
        assert f"sys-{i}" in line


def test_replay_active_run_warning(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-active"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").touch()
    (run_dir / "RUNNING").touch()

    result = runner.invoke(app, ["replay", run_id])

    assert result.exit_code == 0, result.stderr
    stderr_lower = result.stderr.lower()
    assert "active" in stderr_lower or "warning" in stderr_lower


def test_replay_completed_run_no_warning(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-done"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").touch()

    result = runner.invoke(app, ["replay", run_id])

    assert result.exit_code == 0, result.stderr
    assert "active" not in result.stderr.lower()


def test_replay_missing_run(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["replay", "run-missing"])

    assert result.exit_code == 2
    assert "No events found for run run-missing" in result.stderr
    assert "vsm runs" in result.stderr
