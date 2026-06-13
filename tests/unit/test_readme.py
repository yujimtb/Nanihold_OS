"""Unit tests for project documentation.

Validates the documentation set after the docs/ reorganization: a lightweight
root README that points at docs/, plus the split reference documents under
docs/. (Originally Requirements: 14.9, now covered by docs/implementation-status.md.)
"""
from __future__ import annotations
import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent
_README_PATH = _ROOT / "README.md"
_DOCS = _ROOT / "docs"
_ARCH_PATH = _DOCS / "architecture.md"
_STATUS_PATH = _DOCS / "implementation-status.md"
_ROADMAP_PATH = _DOCS / "roadmap.md"


def _read(path: Path) -> str:
    assert path.exists(), f"expected documentation file not found: {path}"
    return path.read_text(encoding="utf-8")


def test_readme_exists():
    assert _README_PATH.exists(), f"README.md not found at {_README_PATH}"


def test_readme_mentions_quick_start_or_install():
    """README has install or quick-start guidance."""
    content = _read(_README_PATH)
    assert "pip install" in content or "Quick" in content or "クイック" in content


def test_readme_links_to_docs():
    """The lightened README points readers at the split docs."""
    content = _read(_README_PATH)
    for link in (
        "docs/setup.md",
        "docs/cli.md",
        "docs/architecture.md",
        "docs/implementation-status.md",
        "docs/roadmap.md",
    ):
        assert link in content, f"README must link to {link}"


def test_core_docs_exist():
    """The reorganized documentation set is present under docs/."""
    for path in (
        _DOCS / "README.md",
        _DOCS / "setup.md",
        _DOCS / "cli.md",
        _DOCS / "web-ui.md",
        _DOCS / "discord-bot.md",
        _ARCH_PATH,
        _STATUS_PATH,
        _ROADMAP_PATH,
    ):
        assert path.exists(), f"expected doc missing: {path}"


def test_status_doc_has_current_scope_and_roadmap_section():
    """The scope/roadmap section lives in docs/implementation-status.md."""
    content = _read(_STATUS_PATH)
    assert re.search(r"^##\s+.*Current Scope and Roadmap", content, re.MULTILINE), (
        "implementation-status.md must contain a 'Current Scope and Roadmap' section"
    )


def test_status_doc_lists_seven_scope_items():
    """Each of REQ 14.1〜14.7 is referenced in the scope doc."""
    content = _read(_STATUS_PATH)
    for req_num in ("14.1", "14.2", "14.3", "14.4", "14.5", "14.6", "14.7"):
        assert f"REQ {req_num}" in content or req_num in content, (
            f"REQ {req_num} not mentioned in implementation-status.md"
        )


def test_status_doc_mentions_key_scope_keywords():
    content = _read(_STATUS_PATH)
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
        assert kw in content, f"keyword {kw!r} missing from implementation-status.md"


def test_status_doc_mentions_refactor_tool_statuses():
    """The status doc lists implementation status for the named tools."""
    content = _read(_STATUS_PATH)
    keywords = [
        "Tool 群の実装状況",
        "llm_call",
        "codex_run",
        "claude_code_run",
        "web_crawl",
        "file_io",
        "spawn_child",
        "search_past_subtasks",
        "request_human_review",
        "terminate_node",
        "suspend_node",
        "resume_node",
    ]
    for kw in keywords:
        assert kw in content, f"keyword {kw!r} missing from implementation-status.md"


def test_docs_describe_recursive_vsm_architecture():
    """The recursive u-VSM model and tool boundary are documented under docs/."""
    corpus = _read(_ARCH_PATH) + "\n" + _read(_STATUS_PATH)
    keywords = [
        "u-VSM",
        "DifferentiationLevel",
        "ParentAuthority",
        "ToolInvocation",
        "differentiate",
        "LiveTopology",
        "Event_Log",
        "spawn 直後",
        "S5 Agent",
        "実体化していない部分",
    ]
    for kw in keywords:
        assert kw in corpus, f"keyword {kw!r} missing from architecture/implementation docs"
