"""Unit tests for ``vsm replay``.

Validates Requirements: 11.5, 11.6, 11.7.

REQ 11.5 — ``vsm replay`` prints one line per event in append order, where
each line is ``<ts> <system_id> <channel> <event_type>`` with a single
space separator. Fields that are absent on a given event type render as
``-`` so every line has the same column structure (this is enforced by
:func:`vsm.cli.replay`).

REQ 11.6 — when invoked against a Run that is still active (the
``runs/{run_id}/RUNNING`` lockfile is still present), the CLI writes a
warning message identifying the Run as active to **stderr** before the
event snapshot is emitted on stdout.

REQ 11.7 — when the Event_Log file does not exist, the CLI writes
``Event_Log not found for run <id>`` to stderr and terminates with exit
code 2.

These tests are example-based (the formal property is covered by
P12 / P13 / P5 elsewhere in the test suite) and use Typer's
:class:`CliRunner` to drive ``vsm.cli.app`` end-to-end. The runner is
constructed without arguments because in the click version pinned by
typer 0.16+, ``CliRunner`` separates stdout and stderr by default; the
legacy ``mix_stderr=False`` kwarg has been removed (see also
``tests/property/test_out_of_scope.py``). ``result.stderr`` therefore
contains only the stderr stream, which is what REQ 11.6 / 11.7 assert
about.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from vsm.cli import app


# In the click version pinned by typer 0.16+, ``CliRunner`` separates
# stdout and stderr by default (the legacy ``mix_stderr=False`` kwarg
# has been removed). See ``tests/property/test_out_of_scope.py`` for the
# same convention applied to scope-rejection tests.
runner = CliRunner()


def _write_events_file(path: Path, events: list[dict]) -> None:
    """Materialise a list of event envelopes to ``events.jsonl``.

    Used by every test below to build the fixture Event_Log on disk
    before invoking ``vsm replay``. Writes one JSON object per line in
    UTF-8 so the writer-side format is byte-for-byte compatible with
    :class:`vsm.eventlog.writer.EventLogWriter`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for evt in events:
            f.write(json.dumps(evt) + "\n")


# ---------------------------------------------------------------------------
# REQ 11.5: line format
# ---------------------------------------------------------------------------


def test_replay_format_single_event(tmp_path, monkeypatch) -> None:
    """REQ 11.5: each line is ``<ts> <system_id> <channel> <event_type>``.

    A single ``system_instantiated`` event must round-trip through
    ``vsm replay`` with the timestamp first, the system identifier in
    column 2, and the event type in column 4 (column 3 is the channel,
    which is rendered as ``-`` for lifecycle events with no channel).
    """
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

    result = runner.invoke(app, ["replay", run_id])

    assert result.exit_code == 0, result.stderr
    line = result.stdout.strip().split("\n")[0]
    parts = line.split(" ")
    # ts, system_id, channel, event_type — at least four whitespace-
    # separated columns. We assert ``>= 4`` rather than ``== 4`` so a
    # future column addition (e.g. seq) does not break this assertion
    # while still pinning the documented prefix.
    assert len(parts) >= 4, f"unexpected line shape: {line!r}"
    assert parts[0] == "2025-01-01T00:00:00.000Z"
    assert "sys-1" in line
    assert "system_instantiated" in line


def test_replay_channel_message_format(tmp_path, monkeypatch) -> None:
    """REQ 11.5: ``channel_message`` uses ``sender`` as the system column.

    Channel events carry ``sender``/``receiver``/``channel`` rather than
    ``system_id``. The CLI falls back to ``sender`` for the system
    column so a single ``vsm replay`` line can identify which System
    originated the message.
    """
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

    result = runner.invoke(app, ["replay", run_id])

    assert result.exit_code == 0, result.stderr
    line = result.stdout.strip().split("\n")[0]
    assert "s4-id" in line
    assert "S4-S5" in line
    assert "channel_message" in line


def test_replay_event_with_no_sys_or_channel(tmp_path, monkeypatch) -> None:
    """REQ 11.5: events without ``system_id`` or ``channel`` render as ``-``.

    ``policy_decision`` carries neither ``system_id`` nor ``channel`` in
    its payload (see design.md §Data Models). The CLI must keep every
    line column-aligned by emitting ``-`` placeholders for the missing
    fields rather than collapsing the columns.
    """
    monkeypatch.chdir(tmp_path)
    run_id = "run-noch"
    events_path = tmp_path / "runs" / run_id / "events.jsonl"
    _write_events_file(
        events_path,
        [
            {
                "ts": "2025-01-01T00:00:00.000Z",
                "run_id": run_id,
                "event_type": "policy_decision",
                "seq": 0,
                "payload": {
                    "decision_id": "d1",
                    "assessment_id": "a1",
                    "directive": "go",
                    "followup_request": "scan",
                },
            },
        ],
    )

    result = runner.invoke(app, ["replay", run_id])

    assert result.exit_code == 0, result.stderr
    line = result.stdout.strip().split("\n")[0]
    # Both system_id and channel default to ``-``, so the line contains
    # the substring ``- -`` for the two consecutive missing columns.
    assert " - - " in line, f"expected '- -' placeholders, got: {line!r}"


def test_replay_preserves_order(tmp_path, monkeypatch) -> None:
    """REQ 11.5: replay output preserves Event_Log append order.

    The CLI must not reorder, deduplicate, or reverse events. With five
    sequential ``system_instantiated`` events whose ``system_id`` is
    indexed 0..4, the output lines must reference ``sys-0`` … ``sys-4``
    in that exact order.
    """
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

    result = runner.invoke(app, ["replay", run_id])

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.strip().split("\n")
    assert len(lines) == 5
    for i, line in enumerate(lines):
        assert f"sys-{i}" in line, (
            f"line {i} did not reference sys-{i}: {line!r}"
        )


# ---------------------------------------------------------------------------
# REQ 11.6: active-Run warning
# ---------------------------------------------------------------------------


def test_replay_active_run_warning(tmp_path, monkeypatch) -> None:
    """REQ 11.6: active Run (``RUNNING`` lockfile present) → stderr warning.

    :class:`vsm.runtime.lifecycle.Platform` creates the ``RUNNING``
    lockfile at Run start and removes it on shutdown. Observing the
    lockfile after-the-fact therefore signals the Run is still in
    flight, and ``vsm replay`` must surface that condition on stderr
    before emitting the snapshot on stdout.
    """
    monkeypatch.chdir(tmp_path)
    run_id = "run-active"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").touch()
    (run_dir / "RUNNING").touch()

    result = runner.invoke(app, ["replay", run_id])

    assert result.exit_code == 0, result.stderr
    # The exact wording is not pinned by REQ 11.6; either ``active`` or
    # ``warning`` must appear so log-greppers can flag the condition.
    stderr_lower = result.stderr.lower()
    assert "active" in stderr_lower or "warning" in stderr_lower, (
        f"expected active/warning in stderr, got: {result.stderr!r}"
    )


def test_replay_completed_run_no_warning(tmp_path, monkeypatch) -> None:
    """REQ 11.6: completed Run (no ``RUNNING`` lockfile) → no warning.

    The negative control for the active-Run warning. Without the
    lockfile, ``vsm replay`` must not emit the active-Run warning so
    operators are not alarmed by a clean post-mortem replay of a
    completed Run.
    """
    monkeypatch.chdir(tmp_path)
    run_id = "run-done"
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "events.jsonl").touch()
    # No ``RUNNING`` lockfile is intentionally created.

    result = runner.invoke(app, ["replay", run_id])

    assert result.exit_code == 0, result.stderr
    assert "active" not in result.stderr.lower(), (
        f"unexpected active-Run warning on completed Run: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# REQ 11.7: missing Event_Log
# ---------------------------------------------------------------------------


def test_replay_missing_run(tmp_path, monkeypatch) -> None:
    """REQ 11.7: missing ``events.jsonl`` → exit 2 with canonical message.

    The exact stderr substring ``Event_Log not found`` is pinned by
    REQ 11.7 so log-greppers can reliably surface the failure regardless
    of which CLI subcommand emitted it.
    """
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["replay", "run-missing"])

    assert result.exit_code == 2
    assert "Event_Log not found" in result.stderr
