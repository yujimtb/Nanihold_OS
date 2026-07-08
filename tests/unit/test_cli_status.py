"""Unit tests for the human-readable ``vsm status`` output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vsm.cli import app


# typer 0.16+: stdout / stderr are separated by default; ``mix_stderr``
# was removed from CliRunner.__init__. See module docstring.
runner = CliRunner()


def _write_events_file(path: Path, events: list[dict]) -> None:
    """Write ``events`` as a JSONL fixture at ``path``.

    The directory is created on demand so callers do not have to call
    ``mkdir(parents=True)`` before every fixture write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")


def test_status_empty_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty ``events.jsonl`` is still rendered as a readable summary."""
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
    """A single ``task_submitted`` event shows id, state, and description."""
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
                    "description": "hi",
                    "file_paths": [],
                    "submitted_at": "2025-01-01T00:00:00.000Z",
                },
            },
        ],
    )

    result = runner.invoke(app, ["status", run_id])
    assert result.exit_code == 0
    assert "task-abc" in result.stdout
    assert "submitted" in result.stdout
    assert "description: hi" in result.stdout


def test_status_with_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``system_instantiated`` events show role names and Sub_Agent counts."""
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
    assert "sys-1" in result.stdout
    assert "sys-2" in result.stdout
    assert "S5_POLICY" in result.stdout
    assert "S4_SCANNER" in result.stdout
    assert "Sub_Agents: 3" in result.stdout
    assert "Sub_Agents: 2" in result.stdout


def test_status_derives_completed_state_from_s1_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Completed Runs no longer look stuck at the submitted state."""
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
                    "task_id": "task-1",
                    "run_id": run_id,
                    "description": "ship status ux",
                    "file_paths": [],
                    "submitted_at": "2025-01-01T00:00:00.000Z",
                },
            },
            {
                "ts": "2025-01-01T00:00:01.000Z",
                "run_id": run_id,
                "event_type": "s1_completion",
                "seq": 1,
                "payload": {
                    "s1_id": "s1-1",
                    "work_item_id": "work-1",
                    "result": {"success": True},
                },
            },
        ],
    )

    result = runner.invoke(app, ["status", run_id])

    assert result.exit_code == 0
    assert "state: completed" in result.stdout
    assert "from s1_completion" in result.stdout


def test_status_missing_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ 11.7: missing ``events.jsonl`` exits 2 with the canonical message.

    The CLI must not silently produce empty output when a non-existent
    Run is requested; it must surface a typed error on stderr and a
    non-zero exit code so wrappers (CI scripts, agents) can detect the
    failure.
    """
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "run-missing"])
    assert result.exit_code == 2
    assert "No events found for run run-missing" in result.stderr
    assert "vsm runs" in result.stderr
    assert "Event_Log" not in result.stderr


def test_status_invalid_run_id() -> None:
    """REQ 10.2: an empty ``run_id`` is rejected with a non-zero exit code.

    The :func:`vsm.ids.validate_run_id` helper raises ``CLIError`` for
    inputs that violate the 1..64 ASCII rule; the CLI translates that
    into ``typer.Exit(code=2)`` before any I/O. We assert only the
    non-zero exit because the exact code (2) is shared with REQ 11.7
    and is covered separately above.
    """
    result = runner.invoke(app, ["status", ""])
    assert result.exit_code != 0
    assert "Example: vsm status run-1234567890abcdef1234567890abcdef" in result.stderr
    assert "REQ" not in result.stderr


def test_status_output_line_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status output should not regress to raw tuple rows."""
    monkeypatch.chdir(tmp_path)
    run_id = "run-fmt"
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
                    "description": "x",
                    "file_paths": [],
                    "submitted_at": "2025-01-01T00:00:00.000Z",
                },
            },
        ],
    )
    result = runner.invoke(app, ["status", run_id])
    assert result.exit_code == 0
    assert "Run:" in result.stdout
    assert "Tasks:" in result.stdout
    assert "(task-1, submitted)" not in result.stdout
