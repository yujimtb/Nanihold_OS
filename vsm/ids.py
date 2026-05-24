"""Identifier helpers: UUIDv4 generation and ``run_id`` validation.

This module provides the canonical, dependency-free helpers used across the
VSM_Platform whenever a Run identifier, Task identifier, or any other
UUIDv4-based identifier is produced or validated.

References
----------
- REQ 4.6: Tasks and Runs are assigned UUIDv4 identifiers on CLI acceptance.
- REQ 10.2: The VSM_Platform SHALL accept Run identifiers consisting of
  between 1 and 64 ASCII characters.
- REQ 11.7: CLI observation subcommands SHALL reject unknown Run identifiers
  with a non-zero exit code; format-level rejection at parse time is handled
  here via :class:`vsm.errors.CLIError` (``exit_code=2``). The exact stderr
  message ``Event_Log not found for run <id>`` is the CLI's responsibility
  (see ``vsm/cli.py``) and is not produced by this validator.
"""

from __future__ import annotations

import uuid

from vsm.errors import CLIError

__all__ = ["generate_run_id", "validate_run_id", "generate_uuid"]

# REQ 10.2: Run identifiers are bounded to [1, 64] ASCII characters.
_RUN_ID_MIN_LEN = 1
_RUN_ID_MAX_LEN = 64

# Exit code 2 is reserved for CLI input validation failures (see
# design.md §Error Handling and the Exit Code 体系 table).
_CLI_VALIDATION_EXIT_CODE = 2


def generate_run_id() -> str:
    """Return a freshly generated Run identifier.

    The identifier is the concatenation of the literal prefix ``run-`` and the
    32-character lowercase hexadecimal form of a UUIDv4 (total length 36),
    which is comfortably within the [1, 64] ASCII range mandated by REQ 10.2
    and is itself the UUIDv4 form required by REQ 4.6.

    Returns
    -------
    str
        A Run identifier of the form ``run-<32 hex chars>``. The result is
        guaranteed to satisfy :func:`validate_run_id` without raising.
    """
    return f"run-{uuid.uuid4().hex}"


def generate_uuid() -> str:
    """Return a fresh UUIDv4 in 32-character lowercase hexadecimal form.

    Used for Task identifiers, audit finding identifiers, and other
    UUIDv4-typed identifiers required by REQ 4.6 and adjacent acceptance
    criteria across Requirements 5–9.

    Returns
    -------
    str
        A 32-character lowercase hexadecimal UUIDv4 string (no dashes).
    """
    return uuid.uuid4().hex


def validate_run_id(s: str) -> None:
    """Validate a user-supplied Run identifier.

    Enforces the format constraint from REQ 10.2: the identifier must be a
    string of between 1 and 64 ASCII characters inclusive. Non-ASCII
    characters and empty strings are rejected. This function performs only
    format-level validation; existence of the corresponding ``runs/{run_id}/``
    directory or ``events.jsonl`` file is checked separately by the CLI
    observation subcommands per REQ 11.7.

    Parameters
    ----------
    s : str
        The candidate Run identifier to validate.

    Raises
    ------
    CLIError
        With ``exit_code=2`` when ``s`` is empty, longer than 64 characters,
        contains any non-ASCII character, or is not a :class:`str` instance.
    """
    if not isinstance(s, str):
        raise CLIError(
            exit_code=_CLI_VALIDATION_EXIT_CODE,
            message=(
                f"run_id must be a string of 1..64 ASCII characters, "
                f"got {type(s).__name__}"
            ),
        )

    length = len(s)
    if length < _RUN_ID_MIN_LEN:
        raise CLIError(
            exit_code=_CLI_VALIDATION_EXIT_CODE,
            message="run_id must not be empty (REQ 10.2: 1..64 ASCII characters)",
        )
    if length > _RUN_ID_MAX_LEN:
        raise CLIError(
            exit_code=_CLI_VALIDATION_EXIT_CODE,
            message=(
                f"run_id must be at most {_RUN_ID_MAX_LEN} ASCII characters "
                f"(REQ 10.2), got {length}"
            ),
        )
    if not s.isascii():
        raise CLIError(
            exit_code=_CLI_VALIDATION_EXIT_CODE,
            message="run_id must contain only ASCII characters (REQ 10.2)",
        )
