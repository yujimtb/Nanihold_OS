"""Event_Log と Node の参照から短い決定論的 context view を構築する。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from vsm.eventlog.reader import read_all
from vsm.nodes.model import Node
from vsm.tools.search import TaskSummaryIndex

_RECENT_EVENT_LIMIT = 6
_TEXT_LIMIT = 240
_ARTIFACT_LIMIT = 600


class ContextViewBuilder:
    """Node 固有の履歴だけを固定テンプレートへ射影する。"""

    def __init__(
        self,
        *,
        nodes: Mapping[str, Node],
        events_path: Path,
        summary_index: TaskSummaryIndex,
        run_dir: Path,
        recent_event_limit: int = _RECENT_EVENT_LIMIT,
    ) -> None:
        if recent_event_limit <= 0:
            raise ValueError("recent_event_limit は正数でなければなりません")
        self._nodes = nodes
        self._events_path = events_path
        self._summary_index = summary_index
        self._run_dir = run_dir.resolve(strict=False)
        self._recent_event_limit = recent_event_limit

    def build(self, node_id: str, run_id: str) -> str:
        """指定 Run/Node の短い日本語ビューを決定論的に返す。"""
        node = self._nodes.get(node_id)
        if node is None:
            raise KeyError(f"Node が存在しません: {node_id}")
        events = [event for event in read_all(self._events_path) if event.get("run_id") == run_id]
        recent = [event for event in events if _belongs_to_node(event, node_id)]
        recent.sort(key=lambda event: int(event.get("seq", -1)))
        recent = recent[-self._recent_event_limit :]

        parent_directive = "なし"
        if node.parent_id is not None:
            for event in reversed(sorted(events, key=lambda item: int(item.get("seq", -1)))):
                directive = _directive_from_parent(event, node.parent_id, node_id)
                if directive is not None:
                    parent_directive = _short(directive)
                    break

        child_summaries = self._summary_index.list_for_nodes(
            run_id=run_id,
            node_ids=set(node.child_ids),
        )
        event_lines = [_event_line(event) for event in recent] or ["- なし"]
        summary_lines = [
            (
                f"- child={entry.node_id} 達成={'はい' if entry.summary.goal_achieved else 'いいえ'} "
                f"方針={_short(entry.summary.approach)}"
            )
            for entry in child_summaries
        ] or ["- なし"]
        artifact_lines = [
            f"- {artifact_ref}: {self._read_artifact(artifact_ref)}"
            for artifact_ref in sorted(node.artifact_refs)
        ] or ["- なし"]

        return "\n".join(
            [
                "【現在の文脈】",
                f"Run: {run_id}",
                f"Node: {node_id} ({_role_name(node)})",
                f"目標: {_short(node.goal) if node.goal else '未設定'}",
                "【親からの指示】",
                parent_directive,
                "【直近イベント】",
                *event_lines,
                "【直接 child の要約】",
                *summary_lines,
                "【参照 Artifact】",
                *artifact_lines,
            ]
        )

    def _read_artifact(self, artifact_ref: str) -> str:
        path = (self._run_dir / artifact_ref).resolve(strict=False)
        if not path.is_relative_to(self._run_dir):
            raise ValueError(f"Artifact 参照が Run ディレクトリ外です: {artifact_ref}")
        text = path.read_text(encoding="utf-8")
        return _short(" ".join(text.split()), _ARTIFACT_LIMIT)


def _role_name(node: Node) -> str:
    value = node.vsm_position
    return value.value if hasattr(value, "value") else str(value)


def _belongs_to_node(event: Mapping[str, Any], node_id: str) -> bool:
    if event.get("node_id") == node_id:
        return True
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return False
    return any(payload.get(key) == node_id for key in ("node_id", "system_id", "s1_id"))


def _directive_from_parent(
    event: Mapping[str, Any], parent_id: str, node_id: str
) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    directive = _find_directive(payload)
    if directive is None:
        return None
    actor_matches = event.get("actor_id") == parent_id or event.get("node_id") == parent_id
    receiver = payload.get("receiver_id")
    target_matches = receiver is None or receiver == node_id
    return directive if actor_matches and target_matches else None


def _find_directive(value: Mapping[str, Any]) -> str | None:
    direct = value.get("directive")
    if isinstance(direct, str) and direct.strip():
        return direct
    nested = value.get("payload")
    return _find_directive(nested) if isinstance(nested, Mapping) else None


def _event_line(event: Mapping[str, Any]) -> str:
    event_type = str(event.get("event_type", "unknown"))
    payload = event.get("payload")
    detail = _event_detail(payload) if isinstance(payload, Mapping) else ""
    suffix = f": {detail}" if detail else ""
    return f"- {event_type}{suffix}"


def _event_detail(payload: Mapping[str, Any]) -> str:
    for key in ("directive", "response", "reason", "status", "text", "result"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _short(value)
        if isinstance(value, Mapping):
            text = value.get("text")
            if isinstance(text, str) and text.strip():
                return _short(text)
    compact = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _short(compact)


def _short(value: str, limit: int = _TEXT_LIMIT) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"
