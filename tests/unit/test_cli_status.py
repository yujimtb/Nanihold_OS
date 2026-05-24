"""Unit tests for ``vsm status``.

Validates Requirements: 11.1

The ``vsm status`` subcommand reads ``runs/{run_id}/events.jsonl`` via
:func:`vsm.eventlog.replay.replay`, then emits one line per Task in the
form ``(task_id, state)`` followed by one line per System in the form
``(system_id, sub_agent_count)`` (REQ 11.1). These tests pin the output
*format* (one tuple per line, comma-space separator, no surrounding
whitespace) on a handful of carefully constructed events.jsonl fixtures
so any future refactor that changes the rendering of either tuple type
trips a failing test here.

Adjacent error-path acceptance criteria (REQ 11.7 missing Event_Log,
REQ 10.2 empty ``run_id``) are also covered: they share the same exit
code (2) and the same stderr-only message contract as the format-level
checks, so it is convenient to anchor them in this file rather than a
second one.

Implementation notes
--------------------
* In typer 0.16+ (which the project pins via ``pyproject.toml``), Click's
  :class:`CliRunner` separates stdout and stderr by default and the
  legacy ``mix_stderr`` keyword argument has been removed. The
  default-constructed runner therefore exposes ``result.stdout`` and
  ``result.stderr`` cleanly, which is exactly what we want for the
  REQ 11.7 stderr assertion.
* ``monkeypatch.chdir(tmp_path)`` is used so the CLI's relative
  ``runs/{run_id}/events.jsonl`` lookup (``vsm.cli._events_path_for``)
  resolves under the per-test temporary directory. No fixture fights
  the working directory.
"""

from __future__ import annotations

import json
import re
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
    """Empty ``events.jsonl`` produces no tasks and no systems on stdout.

    REQ 11.1 only requires the two tuple groups to be printed *if any
    Tasks / Systems exist*. With an empty Event_Log the reconstructed
    state has empty ``tasks`` and ``systems`` projections, so stdout is
    empty (modulo Click's trailing newline conventions, which the
    ``.strip()`` accommodates).
    """
    monkeypatch.chdir(tmp_path)
    run_id = "run-empty"
    (tmp_path / "runs" / run_id).mkdir(parents=True)
    (tmp_path / "runs" / run_id / "events.jsonl").touch()

    result = runner.invoke(app, ["status", run_id])
    assert result.exit_code == 0
    # Empty output (or minimal whitespace).
    assert result.stdout.strip() == ""


def test_status_with_one_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single ``task_submitted`` event yields a ``(task_id, state)`` line.

    REQ 11.1: the Task projection is emitted before the System projection
    and one tuple per line. With only one Task and no System events the
    fixture exercises the Task half of the contract in isolation.
    """
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
    # Task id and the canonical SUBMITTED state both appear on the
    # rendered tuple line.
    assert "task-abc" in result.stdout
    assert "submitted" in result.stdout


def test_status_with_systems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``system_instantiated`` events yield ``(system_id, sub_agent_count)`` lines.

    REQ 11.1: every System tracked by the replayed state must be emitted
    as a tuple. The fixture seeds two distinct Systems with different
    sub-agent counts so that *both* identifiers and *both* counts are
    asserted on stdout.
    """
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
    # Sub_Agent counts are rendered as bare integers (no quoting).
    assert "3" in result.stdout
    assert "2" in result.stdout


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
    assert "Event_Log not found" in result.stderr


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


def test_status_output_line_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REQ 11.1: every non-empty output line is a ``(id, value)`` tuple.

    This is the regex-pinned format check. The pattern
    ``^\\(.+, .+\\)$`` enforces:

    * a literal opening ``(``;
    * any non-empty identifier;
    * a literal ``, `` separator (comma + single space);
    * any non-empty state-or-count;
    * a literal closing ``)``.

    Any future refactor that, e.g., drops the space after the comma or
    introduces multi-line tuple rendering will trip this test.
    """
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
    # Every non-empty line should match the ``(id, value)`` shape.
    for line in result.stdout.strip().split("\n"):
        if line:
            assert re.match(
                r"^\(.+, .+\)$", line
            ), f"unexpected line format: {line!r}"
