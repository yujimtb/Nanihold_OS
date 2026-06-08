# Feature: Nanihold OS, Property 13: CLI input validation (run_id portion)
"""Property-based tests for ``vsm.ids`` (run_id boundary).

This module implements the ``run_id`` portion of **Property 13: CLI input
validation** from design.md §Correctness Properties.

**Validates: Requirements 10.2, 11.7**

Concretely, the tests below use Hypothesis to verify that
:func:`vsm.ids.validate_run_id` enforces the format constraint declared in
REQ 10.2 (Run identifiers consist of between 1 and 64 ASCII characters) and
that violations are surfaced as :class:`vsm.errors.CLIError` with
``exit_code=2`` so the CLI layer can terminate with the non-zero exit code
mandated by REQ 11.7. Additionally, :func:`vsm.ids.generate_run_id` and
:func:`vsm.ids.generate_uuid` are exercised to confirm they always produce
identifiers that satisfy the validator (REQ 4.6 supports REQ 10.2).

Each ``@given`` test is constrained by ``@settings(max_examples=100)`` per
the project-wide PBT convention recorded in tasks.md.
"""

from __future__ import annotations

import re
import string

import pytest
from hypothesis import given, settings, strategies as st

from vsm.errors import CLIError
from vsm.ids import generate_run_id, generate_uuid, validate_run_id

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# REQ 10.2: valid run_ids are 1..64 ASCII characters.
_VALID_RUN_ID = st.text(
    alphabet=st.characters(min_codepoint=0, max_codepoint=127),
    min_size=1,
    max_size=64,
)

# REQ 10.2 boundary: anything strictly longer than 64 ASCII chars must be
# rejected. Capping at 200 keeps Hypothesis exploration tractable while still
# covering values well beyond the boundary.
_TOO_LONG_RUN_ID = st.text(
    alphabet=st.characters(min_codepoint=0, max_codepoint=127),
    min_size=65,
    max_size=200,
)

# REQ 10.2: even within the [1, 64] length window, any non-ASCII codepoint
# must be rejected. We sample from the BMP above U+007F.
_NON_ASCII_RUN_ID = st.text(
    alphabet=st.characters(min_codepoint=128, max_codepoint=0xFFFF),
    min_size=1,
    max_size=64,
)

# Pre-compiled matcher for the 32-char lowercase hex form returned by
# :func:`generate_uuid` (UUIDv4 hex per REQ 4.6).
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# validate_run_id: rejection cases (REQ 10.2 → CLIError(exit_code=2))
# ---------------------------------------------------------------------------


def test_validate_run_id_rejects_empty_string() -> None:
    """REQ 10.2: empty string is rejected with ``exit_code=2``.

    The empty string is the lower-boundary violation (length 0 < 1) and is
    handled as a single deterministic example rather than via Hypothesis.
    """
    with pytest.raises(CLIError) as exc_info:
        validate_run_id("")
    assert exc_info.value.exit_code == 2


@given(s=_TOO_LONG_RUN_ID)
@settings(max_examples=100)
def test_validate_run_id_rejects_too_long(s: str) -> None:
    """REQ 10.2: any ASCII string longer than 64 characters is rejected.

    The strategy generates only ASCII characters so that the length check is
    the sole reason for rejection (isolating the > 64 boundary).
    """
    assert len(s) > 64
    assert s.isascii()
    with pytest.raises(CLIError) as exc_info:
        validate_run_id(s)
    assert exc_info.value.exit_code == 2


@given(s=_NON_ASCII_RUN_ID)
@settings(max_examples=100)
def test_validate_run_id_rejects_non_ascii(s: str) -> None:
    """REQ 10.2: any string containing non-ASCII characters is rejected.

    Length is constrained to [1, 64] so the only failing predicate is the
    ASCII-only requirement.
    """
    assert 1 <= len(s) <= 64
    assert not s.isascii()
    with pytest.raises(CLIError) as exc_info:
        validate_run_id(s)
    assert exc_info.value.exit_code == 2


# ---------------------------------------------------------------------------
# validate_run_id: acceptance cases (REQ 10.2 in-bounds)
# ---------------------------------------------------------------------------


@given(s=_VALID_RUN_ID)
@settings(max_examples=100)
def test_validate_run_id_accepts_in_range_ascii(s: str) -> None:
    """REQ 10.2: every 1..64-char ASCII string is accepted (no exception).

    :func:`validate_run_id` returns ``None`` on success, so the property is
    simply that the call does not raise for any in-range ASCII string.
    """
    assert 1 <= len(s) <= 64
    assert s.isascii()
    # Should not raise.
    assert validate_run_id(s) is None


@pytest.mark.parametrize(
    "s",
    [
        "a",  # minimum length (1)
        "x" * 64,  # maximum length (64)
        "run-0123456789abcdef",  # canonical generate_run_id-like prefix form
        # Printable ASCII mix kept within the [1, 64] window (REQ 10.2).
        (string.ascii_letters + string.digits + "-_.+/=")[:64],
    ],
)
def test_validate_run_id_accepts_known_boundary_examples(s: str) -> None:
    """REQ 10.2: explicit boundary examples (length 1, length 64, mixed ASCII).

    These deterministic examples complement the Hypothesis-driven test by
    pinning the exact lower and upper length bounds.
    """
    assert validate_run_id(s) is None


# ---------------------------------------------------------------------------
# generate_run_id (REQ 4.6 + REQ 10.2)
# ---------------------------------------------------------------------------


@given(_=st.integers(min_value=0, max_value=99))
@settings(max_examples=100)
def test_generate_run_id_has_run_prefix_and_valid_length(_: int) -> None:
    """REQ 4.6 / REQ 10.2: ``generate_run_id`` returns a ``run-``-prefixed
    string whose length lies within [1, 64].

    The Hypothesis-generated integer is unused; it merely drives the
    framework to repeatedly invoke :func:`generate_run_id` and assert the
    invariant on independent samples (UUIDv4 randomness comes from
    :mod:`uuid`).
    """
    rid = generate_run_id()
    assert isinstance(rid, str)
    assert rid.startswith("run-")
    assert 1 <= len(rid) <= 64


@given(_=st.integers(min_value=0, max_value=99))
@settings(max_examples=100)
def test_generate_run_id_passes_validate_run_id(_: int) -> None:
    """REQ 4.6 + REQ 10.2: every ``generate_run_id()`` output passes
    :func:`validate_run_id` without raising.

    This is the round-trip property between the generator and the validator
    relied upon by ``vsm submit`` (REQ 4.6) and the CLI observation
    subcommands (REQ 11.7).
    """
    rid = generate_run_id()
    # Should not raise.
    assert validate_run_id(rid) is None


# ---------------------------------------------------------------------------
# generate_uuid (REQ 4.6)
# ---------------------------------------------------------------------------


@given(_=st.integers(min_value=0, max_value=99))
@settings(max_examples=100)
def test_generate_uuid_returns_32_char_lowercase_hex(_: int) -> None:
    """REQ 4.6: ``generate_uuid`` returns a 32-character lowercase hex
    UUIDv4 string (no dashes).

    The Hypothesis-generated integer is unused; it drives repeated
    invocation so the format invariant is checked across many samples.
    """
    u = generate_uuid()
    assert isinstance(u, str)
    assert len(u) == 32
    assert _HEX32_RE.match(u) is not None
    # Lowercase invariant: no uppercase hex digits.
    assert u == u.lower()
