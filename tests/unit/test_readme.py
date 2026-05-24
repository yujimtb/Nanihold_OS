"""Unit test for README. Validates Requirements: 14.9."""
from __future__ import annotations
import re
from pathlib import Path
import pytest


_README_PATH = Path(__file__).parent.parent.parent / "README.md"


def test_readme_exists():
    assert _README_PATH.exists(), f"README.md not found at {_README_PATH}"


def test_readme_has_mvp_scope_boundaries_section():
    """REQ 14.9: README has '## MVP Scope Boundaries' header."""
    content = _README_PATH.read_text(encoding="utf-8")
    assert re.search(r"^##\s+MVP Scope Boundaries", content, re.MULTILINE), (
        "README must contain '## MVP Scope Boundaries' section (REQ 14.9)"
    )


def test_readme_lists_seven_scope_outs():
    """REQ 14.9: each of REQ 14.1〜14.7 is referenced in MVP Scope Boundaries."""
    content = _README_PATH.read_text(encoding="utf-8")
    # Find MVP Scope Boundaries section
    match = re.search(r"##\s+MVP Scope Boundaries(.+?)(?=^##\s+|\Z)", content, re.MULTILINE | re.DOTALL)
    assert match, "MVP Scope Boundaries section not found"
    section = match.group(1)
    # Verify all 7 REQ identifiers
    for req_num in ("14.1", "14.2", "14.3", "14.4", "14.5", "14.6", "14.7"):
        assert f"REQ {req_num}" in section or req_num in section, (
            f"REQ {req_num} not mentioned in MVP Scope Boundaries section"
        )


def test_readme_mentions_key_scope_out_keywords():
    """REQ 14.9: key scope-out keywords (FSX, public, surplus, etc.) are present."""
    content = _README_PATH.read_text(encoding="utf-8")
    keywords = ["FSX", "公共性", "共有剰余", "Web UI"]
    for kw in keywords:
        assert kw in content, f"keyword {kw!r} missing from README"


def test_readme_mentions_quick_start_or_install():
    """README has install or quick-start guidance."""
    content = _README_PATH.read_text(encoding="utf-8")
    assert "pip install" in content or "Quick" in content or "クイック" in content
