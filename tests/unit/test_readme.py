"""Unit test for README. Validates Requirements: 14.9."""
from __future__ import annotations
import re
from pathlib import Path


_README_PATH = Path(__file__).parent.parent.parent / "README.md"


def test_readme_exists():
    assert _README_PATH.exists(), f"README.md not found at {_README_PATH}"


def test_readme_has_current_scope_and_roadmap_section():
    """REQ 14.9: README has the current scope and roadmap section."""
    content = _README_PATH.read_text(encoding="utf-8")
    assert re.search(r"^##\s+Current Scope and Roadmap", content, re.MULTILINE), (
        "README must contain '## Current Scope and Roadmap' section (REQ 14.9)"
    )


def test_readme_lists_seven_scope_items():
    """REQ 14.9: each of REQ 14.1〜14.7 is referenced in the scope section."""
    content = _README_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"##\s+Current Scope and Roadmap(.+?)(?=^##\s+|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "Current Scope and Roadmap section not found"
    section = match.group(1)
    # Verify all 7 REQ identifiers
    for req_num in ("14.1", "14.2", "14.3", "14.4", "14.5", "14.6", "14.7"):
        assert f"REQ {req_num}" in section or req_num in section, (
            f"REQ {req_num} not mentioned in MVP Scope Boundaries section"
        )


def test_readme_mentions_key_scope_keywords():
    """REQ 14.9: key scope and roadmap keywords are present."""
    content = _README_PATH.read_text(encoding="utf-8")
    keywords = [
        "FSX",
        "公共性",
        "共有剰余",
        "Web UI",
        "サブ VSM",
        "コード実行",
        "ファイル編集",
        "外部プロセス",
    ]
    for kw in keywords:
        assert kw in content, f"keyword {kw!r} missing from README"


def test_readme_mentions_quick_start_or_install():
    """README has install or quick-start guidance."""
    content = _README_PATH.read_text(encoding="utf-8")
    assert "pip install" in content or "Quick" in content or "クイック" in content
