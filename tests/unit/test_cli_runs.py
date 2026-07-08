"""Unit tests for ``vsm runs``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vsm.cli import app


runner = CliRunner()


def _write_events_file(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")


def _task_event(
    *,
    run_id: str,
    seq: int,
    description: str,
    ts: str,
) -> dict:
    return {
        "ts": ts,
        "run_id": run_id,
        "event_type": "task_submitted",
        "seq": seq,
        "payload": {
            "task_id": f"task-{seq}",
            "run_id": run_id,
            "description": description,
            "file_paths": [],
            "submitted_at": ts,
        },
    }


def test_runs_lists_recent_runs_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    older = "run-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    newer = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

    older_dir = tmp_path / "runs" / older
    newer_dir = tmp_path / "runs" / newer
    _write_events_file(
        older_dir / "events.jsonl",
        [
            _task_event(
                run_id=older,
                seq=0,
                description="old submitted task",
                ts="2025-01-01T00:00:00.000Z",
            )
        ],
    )
    _write_events_file(
        newer_dir / "events.jsonl",
        [
            _task_event(
                run_id=newer,
                seq=0,
                description="new completed task",
                ts="2025-01-02T00:00:00.000Z",
            ),
            {
                "ts": "2025-01-02T00:00:01.000Z",
                "run_id": newer,
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
    os.utime(older_dir, (1000, 1000))
    os.utime(newer_dir, (2000, 2000))

    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 0, result.stderr
    assert "Run list" in result.stdout
    assert "run-aaaaaaaa" in result.stdout
    assert "run-bbbbbbbb" in result.stdout
    assert result.stdout.index("run-aaaaaaaa") < result.stdout.index("run-bbbbbbbb")
    assert "completed" in result.stdout
    assert "new completed task" in result.stdout


def test_runs_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for index in range(3):
        run_id = f"run-{index:032d}"
        run_dir = tmp_path / "runs" / run_id
        _write_events_file(
            run_dir / "events.jsonl",
            [
                _task_event(
                    run_id=run_id,
                    seq=0,
                    description=f"task {index}",
                    ts=f"2025-01-0{index + 1}T00:00:00.000Z",
                )
            ],
        )
        os.utime(run_dir, (1000 + index, 1000 + index))

    result = runner.invoke(app, ["runs", "--limit", "2"])

    assert result.exit_code == 0, result.stderr
    data_lines = [
        line for line in result.stdout.splitlines() if line.startswith("run-")
    ]
    assert len(data_lines) == 2


def test_runs_ignores_directories_without_events_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _write_events_file(
        tmp_path / "runs" / run_id / "events.jsonl",
        [
            _task_event(
                run_id=run_id,
                seq=0,
                description="real run",
                ts="2025-01-01T00:00:00.000Z",
            )
        ],
    )
    (tmp_path / "runs" / "_smoke").mkdir()

    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 0, result.stderr
    assert "run-aaaaaaaa" in result.stdout
    assert "_smoke" not in result.stdout
    assert "missing_event_log" not in result.stdout
    assert "ignored 1 directory without events.jsonl" in result.stdout


def test_runs_state_column_expands_for_long_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    run_id = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    run_dir = tmp_path / "runs" / run_id
    _write_events_file(
        run_dir / "events.jsonl",
        [
            _task_event(
                run_id=run_id,
                seq=0,
                description="active completed task",
                ts="2025-01-01T00:00:00.000Z",
            ),
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
    (run_dir / "RUNNING").touch()

    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    header = next(line for line in lines if line.startswith("RUN ID"))
    data = next(line for line in lines if line.startswith("run-"))
    state_start = header.index("STATE")
    events_start = header.index("EVENTS")
    assert data[state_start:events_start].strip() == "completed+active"


def test_smoke_run_creates_run_visible_to_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    smoke_script = repo_root / "scripts" / "smoke_run.py"

    completed = subprocess.run(
        [sys.executable, "-u", str(smoke_script)],
        cwd=tmp_path,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0

    monkeypatch.chdir(tmp_path)
    run_dirs = sorted((tmp_path / "runs").glob("run-*"))
    assert len(run_dirs) == 1
    run_id = run_dirs[0].name

    runs_result = runner.invoke(app, ["runs", "--full-id"])
    assert runs_result.exit_code == 0, runs_result.stderr
    assert run_id in runs_result.stdout
    assert "smoke test" in runs_result.stdout

    status_result = runner.invoke(app, ["status", run_id])
    assert status_result.exit_code == 0, status_result.stderr
    assert "description: smoke test" in status_result.stdout
    assert "state: completed" in status_result.stdout

    replay_result = runner.invoke(app, ["replay", run_id])
    assert replay_result.exit_code == 0, replay_result.stderr
    assert "task: smoke test" in replay_result.stdout
    assert "llm response:" in replay_result.stdout


def test_runs_without_runs_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["runs"])

    assert result.exit_code == 0
    assert "No Runs found" in result.stdout


def test_runs_rejects_file_as_runs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    not_dir = tmp_path / "runs.txt"
    not_dir.write_text("not a directory", encoding="utf-8")

    result = runner.invoke(app, ["runs", "--runs-dir", str(not_dir)])

    assert result.exit_code == 2
    assert "Runs path is not a directory" in result.stderr
    assert "Example:" in result.stderr
    assert "REQ" not in result.stderr
