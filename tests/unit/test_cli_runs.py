"""Unit tests for ``vsm runs``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from vsm.cli import app


runner = CliRunner()


def _write_events_file(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt, ensure_ascii=False) + "\n")


def _task_event(run_id: str, seq: int, description: str) -> dict:
    return {
        "ts": f"2025-01-01T00:00:00.{seq:03d}Z",
        "run_id": run_id,
        "event_type": "task_submitted",
        "seq": seq,
        "payload": {
            "task_id": f"task-{seq}",
            "run_id": run_id,
            "description": description,
            "file_paths": [],
            "submitted_at": f"2025-01-01T00:00:00.{seq:03d}Z",
        },
    }


def test_runs_without_runs_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 0
    assert "No Runs found under runs" in result.stdout


def test_runs_lists_recent_runs_with_derived_state(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    old_id = "run-old-1234567890abcdef"
    new_id = "run-new-1234567890abcdef"
    _write_events_file(
        runs_dir / old_id / "events.jsonl",
        [_task_event(old_id, 0, "older task")],
    )
    _write_events_file(
        runs_dir / new_id / "events.jsonl",
        [
            _task_event(new_id, 0, "newer task"),
            {
                "ts": "2025-01-01T00:00:00.010Z",
                "run_id": new_id,
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
    os.utime(runs_dir / old_id / "events.jsonl", (1, 1))
    os.utime(runs_dir / new_id / "events.jsonl", (2, 2))

    result = runner.invoke(app, ["runs", "--full-id"])

    assert result.exit_code == 0
    assert "Run list (newest first)" in result.stdout
    assert new_id in result.stdout
    assert old_id in result.stdout
    assert result.stdout.index(new_id) < result.stdout.index(old_id)
    assert "completed" in result.stdout
    assert "newer task" in result.stdout


def test_runs_marks_active_and_ignores_non_run_dirs(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    run_id = "run-active-1234567890abcdef"
    _write_events_file(
        runs_dir / run_id / "events.jsonl",
        [_task_event(run_id, 0, "active task")],
    )
    (runs_dir / run_id / "RUNNING").touch()
    (runs_dir / "notes").mkdir()

    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 0
    assert "submitted+active" in result.stdout
    assert "ignored 1 directory without events.jsonl" in result.stdout


def test_runs_shows_run_budget_totals(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-budget-1234567890abcdef"
    events = [
        _task_event(run_id, 0, "budget task"),
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
    ]
    _write_events_file(tmp_path / "runs" / run_id / "events.jsonl", events)

    result = runner.invoke(app, ["runs", "--full-id"])

    assert result.exit_code == 0
    assert "TOKENS" in result.stdout
    assert "WALL" in result.stdout
    assert "16" in result.stdout
    assert "1.250s" in result.stdout


def test_runs_limit_and_short_id(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runs_dir = tmp_path / "runs"
    first = "run-first-1234567890abcdef"
    second = "run-second-1234567890abcdef"
    _write_events_file(runs_dir / first / "events.jsonl", [_task_event(first, 0, "alpha")])
    _write_events_file(runs_dir / second / "events.jsonl", [_task_event(second, 0, "beta")])
    os.utime(runs_dir / first / "events.jsonl", (1, 1))
    os.utime(runs_dir / second / "events.jsonl", (2, 2))

    result = runner.invoke(app, ["runs", "--limit", "1"])

    assert result.exit_code == 0
    assert "run-second-1" in result.stdout
    assert second not in result.stdout
    assert "beta" in result.stdout
    assert "alpha" not in result.stdout


def test_smoke_run_creates_run_visible_to_cli(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "smoke_run.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        str(repo_root)
        if not env.get("PYTHONPATH")
        else str(repo_root) + os.pathsep + env["PYTHONPATH"]
    )

    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        # 並列ゲート・高負荷時のフレーク回避余裕(単体では10秒未満で完走)
        timeout=240,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    run_dirs = list((tmp_path / "runs").glob("run-*"))
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name

    result = runner.invoke(
        app,
        ["runs", "--runs-dir", str(tmp_path / "runs"), "--full-id"],
    )

    assert result.exit_code == 0
    assert run_id in result.stdout
    assert "smoke test: representative VSM event flow" in result.stdout
