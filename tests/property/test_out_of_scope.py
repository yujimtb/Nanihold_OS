"""Property 17 (Out-of-scope absence and rejection). Validates Requirements: 14.1〜14.8.

Feature: vsm-poc-platform, Property 17
Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7, 14.8

This module implements **Property 17: Out-of-scope absence and rejection**
from design.md §Correctness Properties. The property has two structural
sub-claims that together encode the MVP scope boundary enumerated in
REQ 14.1〜14.7 and the rejection contract specified by REQ 14.8:

1. **Absence (REQ 14.1〜14.7).** None of the seven out-of-MVP-scope
   capability names (``fsx``, ``publicness``, ``shared-surplus``,
   ``human-intervention``, ``recursive-growth``, ``semi-stateful-mix``,
   ``web-ui``) is registered as a Typer subcommand. This is the
   compile-time guarantee that the CLI surface cannot accidentally
   expose a forbidden capability simply by being invoked.

2. **Rejection (REQ 14.8).** When the CLI does receive a token whose
   lowercase form matches one of the out-of-scope names, it terminates
   with exit code 5 and writes the canonical message
   ``requested capability is out of MVP scope: <name>`` to stderr. The
   match is case-insensitive, so ``FSX`` / ``Fsx`` / ``fsX`` are all
   rejected.

The case-insensitive sub-claim is exercised both as a parametrised
example test (one case per scope-out name) and as a Hypothesis-driven
property test that samples names from :data:`vsm.cli.OUT_OF_SCOPE_NAMES`
and asserts the rejection invariant for every common case-folding
variant. Per the project-wide PBT convention recorded in tasks.md, the
property test is bounded by ``@settings(max_examples=50)``: with three
case variants exercised per generated name and only seven names in the
domain, 50 examples comfortably visits every (name, variant) pair many
times over.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st
from typer.testing import CliRunner

from vsm.cli import OUT_OF_SCOPE_NAMES, app


# In the click version pinned by typer 0.16+, ``CliRunner`` separates
# stdout and stderr by default (the legacy ``mix_stderr=False`` kwarg
# has been removed). The default-constructed runner therefore exposes
# ``result.stderr`` cleanly, which lets the rejection-message assertions
# below match on the exact stderr emitted by ``_scope_guard`` without
# having to disambiguate it from other CLI output.
runner = CliRunner()


def test_out_of_scope_names_count() -> None:
    """REQ 14.1〜14.7: exactly seven scope-out capability names.

    The set is intentionally pinned at seven (one per acceptance
    criterion 14.1 through 14.7) so that adding or removing a name
    without updating the requirements document raises a test failure
    here.
    """
    assert len(OUT_OF_SCOPE_NAMES) == 7


def test_registered_commands_disjoint_from_out_of_scope() -> None:
    """REQ 14.1〜14.7: registered Typer commands ∩ OUT_OF_SCOPE_NAMES == ∅.

    Inspects the underlying Click command produced by Typer and asserts
    that none of the seven out-of-scope names appears as a registered
    subcommand. This is the structural absence guarantee: even if
    :func:`_scope_guard` were bypassed, the dispatcher itself has no
    handler for these names, so a forbidden capability cannot be
    invoked through normal CLI use.
    """
    from typer.main import get_command

    click_cmd = get_command(app)
    registered_names: set[str] = set()
    if hasattr(click_cmd, "commands"):
        registered_names = set(click_cmd.commands.keys())
    overlap = registered_names & OUT_OF_SCOPE_NAMES
    assert overlap == set(), f"out-of-scope names registered: {overlap}"


@pytest.mark.parametrize("scope_name", sorted(OUT_OF_SCOPE_NAMES))
def test_each_out_of_scope_name_rejected(scope_name: str) -> None:
    """REQ 14.8: every out-of-scope name is rejected with exit code 5.

    For each canonical (lowercase) scope-out name, invoking ``vsm <name>``
    must:

    * exit with code 5 (the exit-code dedicated to scope violations in
      design.md §Error Handling §Exit Code 体系), and
    * include the substring ``out of MVP scope`` in stderr so log
      pipelines can reliably grep for the rejection signal.
    """
    result = runner.invoke(app, [scope_name])
    assert result.exit_code == 5, (
        f"scope_name={scope_name}, exit={result.exit_code}, "
        f"stderr={result.stderr!r}"
    )
    assert "out of MVP scope" in result.stderr


@given(scope_name=st.sampled_from(sorted(OUT_OF_SCOPE_NAMES)))
@settings(max_examples=50)
def test_out_of_scope_case_insensitive_property(scope_name: str) -> None:
    """REQ 14.8: rejection is case-insensitive across canonical variants.

    For every name in :data:`OUT_OF_SCOPE_NAMES`, the three common
    case-folding variants (lower / upper / title) must all produce exit
    code 5. This is the property-based formulation of the
    case-insensitivity clause in REQ 14.8.
    """
    for variant in (scope_name.lower(), scope_name.upper(), scope_name.title()):
        result = runner.invoke(app, [variant])
        assert result.exit_code == 5, (
            f"variant={variant!r} (orig={scope_name!r}) not rejected: "
            f"exit={result.exit_code}, stderr={result.stderr!r}"
        )


def test_normal_commands_not_rejected() -> None:
    """Sanity: in-scope invocations such as ``vsm --help`` are not rejected.

    Without this negative control the rejection tests above would still
    pass against an over-eager guard that rejected *every* invocation.
    Asserting that ``--help`` exits cleanly with code 0 confirms the
    guard's match set is bounded to the seven scope-out names.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
