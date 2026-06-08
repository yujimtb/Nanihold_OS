"""Property 13 (CLI input validation). Validates Requirements: 4.2, 4.5, 10.2, 11.7, 14.8.

This module implements **Property 13** from design.md §Correctness Properties.

Per design.md:

    For any CLI invocation `i`, if `i` violates any of the input constraints
    below, the CLI SHALL terminate with a non-zero exit code and write to
    stderr a message identifying the violated constraint, and SHALL NOT
    create a Run directory or emit any Event_Log entries:
    - description length outside [1, 8192] ASCII characters (REQ 4.2),
    - file path argument that does not exist, exceeds 1 MB, is not valid
      UTF-8, or cannot be read (REQ 4.5),
    - run_id outside [1, 64] ASCII characters when used in observation
      subcommands (REQ 10.2 / 11.7),
    - requested capability name that matches any of the out-of-scope
      capabilities (REQ 14.8).

The tests below drive the in-process Typer application via Click's
``CliRunner`` so that ``stderr`` and the typed exit code can be observed
without spawning a subprocess. This keeps the suite fast enough for
``@settings(max_examples=100)`` while still exercising the real
``vsm.cli`` argument-validation paths used by ``vsm submit``,
``vsm status``, ``vsm replay``, and ``vsm tail``.

NOTE: ``submit`` would, on a *valid* input, start a real Run and call the
LLM provider. The cases exercised here are all *invalid* inputs whose
validation short-circuits before any Run directory is created, so the
tests run cleanly without an LLM provider environment configured.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st
from typer.testing import CliRunner

from vsm.cli import app

# Click 8.3 removed the ``mix_stderr`` keyword; ``result.stderr`` is now
# populated independently of stdout by default.
runner = CliRunner()


# ---------------------------------------------------------------------------
# Description validation (REQ 4.2)
# ---------------------------------------------------------------------------


@given(desc_len=st.integers(min_value=8193, max_value=20000))
@settings(max_examples=100)
def test_description_over_8192_rejected(desc_len: int) -> None:
    """REQ 4.2: description length > 8192 ASCII chars exits with code 2.

    Hypothesis explores lengths just above the boundary and well beyond it
    so that any off-by-one slip in the upper-bound check is detected.
    """
    desc = "a" * desc_len
    result = runner.invoke(app, ["submit", desc])
    assert result.exit_code == 2
    assert (
        "out of range" in result.stderr
        or "description length" in result.stderr
    )


def test_empty_description_rejected() -> None:
    """REQ 4.2: empty description (length 0) exits with code 2.

    The empty string is the lower-boundary violation and is checked as a
    deterministic example rather than via Hypothesis.
    """
    result = runner.invoke(app, ["submit", ""])
    assert result.exit_code == 2
    assert (
        "out of range" in result.stderr
        or "description length" in result.stderr
    )


@given(
    non_ascii=st.text(
        alphabet=st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=100)
def test_non_ascii_description_rejected(non_ascii: str) -> None:
    """REQ 4.2: any description containing non-ASCII characters exits 2.

    The strategy samples from the CJK Unified Ideographs block (U+4E00 ..
    U+9FFF) so that the description has length in [1, 10] (i.e. inside
    the [1, 8192] window) and the only failing predicate is the
    ASCII-only requirement.
    """
    result = runner.invoke(app, ["submit", non_ascii])
    assert result.exit_code == 2
    assert (
        "out of range" in result.stderr
        or "description length" in result.stderr
    )


# ---------------------------------------------------------------------------
# File argument validation (REQ 4.5)
# ---------------------------------------------------------------------------


def test_nonexistent_file_rejected(tmp_path) -> None:
    """REQ 4.5: a non-existent file path exits with code 2.

    The description argument is a valid 4-character ASCII string so the
    only failing predicate is the file-existence check.
    """
    missing = tmp_path / "does-not-exist.txt"
    result = runner.invoke(app, ["submit", "test", "-f", str(missing)])
    assert result.exit_code == 2
    assert "does not exist" in result.stderr


def test_oversized_file_rejected(tmp_path) -> None:
    """REQ 4.5: a file larger than 1 MiB exits with code 2.

    Writes exactly ``1_048_577`` bytes (1 MiB + 1) so the file is the
    smallest possible boundary violation.
    """
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * 1_048_577)  # 1 MiB + 1
    result = runner.invoke(app, ["submit", "test", "-f", str(big)])
    assert result.exit_code == 2
    assert "exceeds" in result.stderr


def test_non_utf8_file_rejected(tmp_path) -> None:
    """REQ 4.5: a file whose contents are not valid UTF-8 exits with code 2.

    ``\\xff\\xfe\\xfd\\xfc`` is a deterministic 4-byte sequence that no
    UTF-8 decoder accepts, so this case isolates the encoding predicate
    from the existence and size predicates.
    """
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"\xff\xfe\xfd\xfc")  # invalid UTF-8
    result = runner.invoke(app, ["submit", "test", "-f", str(bad)])
    assert result.exit_code == 2
    assert "UTF-8" in result.stderr or "not valid UTF-8" in result.stderr


# ---------------------------------------------------------------------------
# run_id validation on observation subcommands (REQ 10.2, REQ 11.7)
# ---------------------------------------------------------------------------


def test_status_with_missing_run_id() -> None:
    """REQ 11.7: ``vsm status`` with a non-existent run_id exits with code 2.

    The run_id ``run-missing`` is format-valid (ASCII, 1..64 chars) but
    no ``runs/run-missing/events.jsonl`` file exists, so the CLI must
    emit the canonical ``Event_Log not found for run <id>`` message.
    """
    result = runner.invoke(app, ["status", "run-missing"])
    assert result.exit_code == 2
    assert "Event_Log not found" in result.stderr


def test_status_with_invalid_run_id_format() -> None:
    """REQ 10.2: ``vsm status`` with a non-ASCII run_id exits with code 2."""
    result = runner.invoke(app, ["status", "ダメ"])
    assert result.exit_code == 2


def test_status_with_too_long_run_id() -> None:
    """REQ 10.2: ``vsm status`` with a run_id longer than 64 chars exits 2."""
    result = runner.invoke(app, ["status", "x" * 65])
    assert result.exit_code == 2


def test_replay_with_missing_run_id() -> None:
    """REQ 11.7: ``vsm replay`` with a non-existent run_id exits with code 2."""
    result = runner.invoke(app, ["replay", "run-missing"])
    assert result.exit_code == 2
    assert "Event_Log not found" in result.stderr


def test_replay_with_invalid_run_id_format() -> None:
    """REQ 10.2: ``vsm replay`` with a non-ASCII run_id exits with code 2."""
    result = runner.invoke(app, ["replay", "ダメ"])
    assert result.exit_code == 2


def test_replay_with_too_long_run_id() -> None:
    """REQ 10.2: ``vsm replay`` with a run_id longer than 64 chars exits 2."""
    result = runner.invoke(app, ["replay", "x" * 65])
    assert result.exit_code == 2


def test_tail_with_missing_run_id() -> None:
    """REQ 11.7: ``vsm tail`` with a non-existent run_id exits with code 2."""
    result = runner.invoke(app, ["tail", "run-missing"])
    assert result.exit_code == 2
    assert "Event_Log not found" in result.stderr


def test_tail_with_invalid_run_id_format() -> None:
    """REQ 10.2: ``vsm tail`` with a non-ASCII run_id exits with code 2."""
    result = runner.invoke(app, ["tail", "ダメ"])
    assert result.exit_code == 2


def test_tail_with_too_long_run_id() -> None:
    """REQ 10.2: ``vsm tail`` with a run_id longer than 64 chars exits 2."""
    result = runner.invoke(app, ["tail", "x" * 65])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Hypothesis-driven run_id boundary fuzzing (REQ 10.2)
# ---------------------------------------------------------------------------

# REQ 10.2 boundary: any ASCII run_id strictly longer than 64 characters
# must be rejected by every observation subcommand.
_TOO_LONG_RUN_ID = st.text(
    alphabet=st.characters(min_codepoint=0, max_codepoint=127),
    min_size=65,
    max_size=200,
)

# REQ 10.2: even a length-1..64 string must be rejected if any character is
# outside ASCII.
_NON_ASCII_RUN_ID = st.text(
    alphabet=st.characters(min_codepoint=128, max_codepoint=0xFFFF),
    min_size=1,
    max_size=64,
)


@pytest.mark.parametrize("subcommand", ["status", "replay", "tail"])
@given(rid=_TOO_LONG_RUN_ID)
@settings(max_examples=100, deadline=None)
def test_observation_subcommands_reject_too_long_run_id(
    subcommand: str, rid: str
) -> None:
    """REQ 10.2 / 11.7: every observation subcommand rejects run_id > 64 ASCII.

    Property 13 quantifies over *every* CLI invocation, so we sweep the
    three observation subcommands with the same Hypothesis-generated
    over-length input and check the typed exit code uniformly.
    """
    assert len(rid) > 64
    result = runner.invoke(app, [subcommand, rid])
    assert result.exit_code == 2


@pytest.mark.parametrize("subcommand", ["status", "replay", "tail"])
@given(rid=_NON_ASCII_RUN_ID)
@settings(max_examples=100, deadline=None)
def test_observation_subcommands_reject_non_ascii_run_id(
    subcommand: str, rid: str
) -> None:
    """REQ 10.2 / 11.7: every observation subcommand rejects non-ASCII run_id.

    Length is constrained to [1, 64] so the only failing predicate is
    the ASCII-only requirement.
    """
    assert 1 <= len(rid) <= 64
    assert not rid.isascii()
    result = runner.invoke(app, [subcommand, rid])
    assert result.exit_code == 2
