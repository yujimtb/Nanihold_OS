"""Memory and context-view related value objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SearchScope(str, Enum):
    DIRECT_CHILD_SUMMARIES = "DIRECT_CHILD_SUMMARIES"
    PARENT_NODE = "PARENT_NODE"
    S4_NODE = "S4_NODE"
    KNOWLEDGE_INDEX = "KNOWLEDGE_INDEX"


@dataclass(frozen=True)
class TaskSummary:
    goal_achieved: bool
    approach: str
    preconditions: str = ""
    output_pointer: str | None = None
    dead_ends: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    reusability_hints: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextView:
    node_id: str
    run_id: str
    event_refs: tuple[str, ...] = ()
    summary_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    decision_refs: tuple[str, ...] = ()
    search_scope: SearchScope = SearchScope.DIRECT_CHILD_SUMMARIES
