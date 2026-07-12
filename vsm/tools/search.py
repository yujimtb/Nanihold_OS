"""Persistent task-summary search tool facade."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vsm.ids import generate_uuid
from vsm.memory.model import SearchScope, TaskSummary
from vsm.tools.model import ToolEffect, ToolInvocation

if TYPE_CHECKING:
    from vsm.authority import ParentAuthority


@dataclass(frozen=True)
class IndexedTaskSummary:
    summary_id: str
    run_id: str
    node_id: str
    summary: TaskSummary
    scope: SearchScope = SearchScope.DIRECT_CHILD_SUMMARIES

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self.summary)
        payload["dead_ends"] = list(self.summary.dead_ends)
        payload["open_questions"] = list(self.summary.open_questions)
        payload["reusability_hints"] = list(self.summary.reusability_hints)
        return {
            "summary_id": self.summary_id,
            "run_id": self.run_id,
            "node_id": self.node_id,
            "scope": self.scope.value,
            "summary": payload,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "IndexedTaskSummary":
        summary_payload = dict(payload["summary"])
        summary_payload["dead_ends"] = tuple(summary_payload.get("dead_ends", ()))
        summary_payload["open_questions"] = tuple(summary_payload.get("open_questions", ()))
        summary_payload["reusability_hints"] = tuple(summary_payload.get("reusability_hints", ()))
        return cls(
            summary_id=payload["summary_id"],
            run_id=payload["run_id"],
            node_id=payload["node_id"],
            scope=SearchScope(payload.get("scope", SearchScope.DIRECT_CHILD_SUMMARIES.value)),
            summary=TaskSummary(**summary_payload),
        )


@dataclass(frozen=True)
class SearchPastSubtasksRequest:
    query: str
    requested_by: str
    index_path: Path
    scope: SearchScope = SearchScope.DIRECT_CHILD_SUMMARIES
    limit: int = 10

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query is required")
        if not self.requested_by.strip():
            raise ValueError("requested_by is required")
        if self.limit <= 0:
            raise ValueError("limit must be positive")
        if not isinstance(self.scope, SearchScope):
            object.__setattr__(self, "scope", SearchScope(self.scope))


class TaskSummaryIndex:
    """JSONL-backed persistent index for ``TaskSummary`` records."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, entry: IndexedTaskSummary) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_payload(), ensure_ascii=False, sort_keys=True))
            fh.write("\n")

    def search(self, query: str, scope: SearchScope, limit: int) -> list[IndexedTaskSummary]:
        needle = query.casefold()
        results: list[IndexedTaskSummary] = []
        for entry in self._read_entries():
            if entry.scope is not scope:
                continue
            if needle in _search_text(entry).casefold():
                results.append(entry)
            if len(results) >= limit:
                break
        return results

    def list_for_nodes(
        self,
        *,
        run_id: str,
        node_ids: set[str],
    ) -> list[IndexedTaskSummary]:
        """Return summaries for the selected nodes in deterministic order."""
        return sorted(
            (
                entry
                for entry in self._read_entries()
                if entry.run_id == run_id and entry.node_id in node_ids
            ),
            key=lambda entry: (entry.node_id, entry.summary_id),
        )

    def _read_entries(self) -> list[IndexedTaskSummary]:
        if not self.path.exists():
            return []
        entries: list[IndexedTaskSummary] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(IndexedTaskSummary.from_payload(json.loads(line)))
        return entries


class SearchPastSubtasksFacade:
    """Search a persistent task-summary index."""

    def search(
        self,
        request: SearchPastSubtasksRequest,
        authority: ParentAuthority | None = None,
    ) -> tuple[ToolInvocation, list[IndexedTaskSummary]]:
        if authority is not None and not authority.allows_tool_effect(ToolEffect.PURE_READ):
            raise PermissionError("search_past_subtasks effect is denied by authority: PURE_READ")
        index = TaskSummaryIndex(request.index_path)
        results = index.search(request.query, request.scope, request.limit)
        invocation = ToolInvocation(
            invocation_id=generate_uuid(),
            tool_name="search_past_subtasks",
            effect=ToolEffect.PURE_READ,
            requested_by_node_id=request.requested_by,
            payload={
                "query": request.query,
                "index_path": str(request.index_path),
                "scope": request.scope.value,
                "limit": request.limit,
                "result": [entry.to_payload() for entry in results],
            },
        )
        return invocation, results


def _search_text(entry: IndexedTaskSummary) -> str:
    summary = entry.summary
    parts = [
        entry.summary_id,
        entry.run_id,
        entry.node_id,
        summary.approach,
        summary.preconditions,
        summary.output_pointer or "",
        *summary.dead_ends,
        *summary.open_questions,
        *summary.reusability_hints,
    ]
    return "\n".join(parts)
