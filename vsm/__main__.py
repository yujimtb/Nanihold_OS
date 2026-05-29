"""Module entry point for ``python -m vsm``."""

from __future__ import annotations

from vsm.cli import app


def main() -> None:
    """Run the Typer CLI application."""
    app()


if __name__ == "__main__":
    main()
