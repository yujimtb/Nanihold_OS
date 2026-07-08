"""CLI process entry-point tests."""

from __future__ import annotations

import subprocess
import sys

import pytest
from typer.testing import CliRunner

from vsm.cli import app


runner = CliRunner()


def test_python_module_entrypoint_prints_help() -> None:
    """``python -m vsm`` should dispatch to the Typer CLI."""
    result = subprocess.run(
        [sys.executable, "-m", "vsm", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "submit" in result.stdout
    assert "runs" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["submit", "--help"],
        ["runs", "--help"],
        ["status", "--help"],
        ["tail", "--help"],
        ["replay", "--help"],
    ],
)
def test_cli_help_is_plain_user_text(args: list[str]) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 0
    assert "REQ" not in result.stdout
    assert ":func:" not in result.stdout
    assert "``" not in result.stdout
    assert "Example" in result.stdout or args == ["--help"]
