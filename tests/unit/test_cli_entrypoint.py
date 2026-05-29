"""CLI process entry-point tests."""

from __future__ import annotations

import subprocess
import sys


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
