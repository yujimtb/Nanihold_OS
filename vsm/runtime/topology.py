"""Static seed topology and live topology projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vsm.nodes import DifferentiationLevel, Node, NodeSource
from vsm.roles import SystemRole


@dataclass(frozen=True)
class StaticTopologyEntry:
    id: str
    role: SystemRole | str
    parent: str | None = None
    terminable: bool = False
    differentiation_level: DifferentiationLevel = DifferentiationLevel.COLLAPSED
    delegates_to: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_node(self) -> Node:
        return Node(
            id=self.id,
            parent_id=self.parent,
            vsm_position=self.role,
            terminable=self.terminable,
            differentiation_level=self.differentiation_level,
            source=NodeSource.CONFIG,
            predefined_children=self.delegates_to,
        )


@dataclass
class LiveTopology:
    nodes: dict[str, Node] = field(default_factory=dict)
    applied_event_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_static(cls, entries: list[StaticTopologyEntry]) -> "LiveTopology":
        topology = cls()
        for entry in entries:
            node = entry.to_node()
            topology.nodes[node.id] = node
        for node in topology.nodes.values():
            if node.parent_id and node.parent_id in topology.nodes:
                topology._append_child_once(node.parent_id, node.id)
        return topology

    def apply_event(self, event: dict[str, Any]) -> None:
        event_id = event.get("event_id")
        if event_id is not None:
            if event_id in self.applied_event_ids:
                return
            self.applied_event_ids.add(event_id)

        payload = event.get("payload") or {}
        event_type = event.get("event_type")
        if event_type == "node_created":
            node = Node(
                id=payload["node_id"],
                parent_id=payload.get("parent_id"),
                vsm_position=payload.get("vsm_position", ""),
                terminable=payload.get("terminable", True),
                differentiation_level=DifferentiationLevel(
                    payload.get("differentiation_level", DifferentiationLevel.COLLAPSED.value)
                ),
                source=NodeSource(payload.get("source", "spawn" if payload.get("terminable", True) else "config")),
            )
            self.nodes[node.id] = node
            if node.parent_id and node.parent_id in self.nodes:
                self._append_child_once(node.parent_id, node.id)
        elif event_type == "node_differentiated":
            node = self.nodes.get(payload.get("node_id"))
            if node is not None:
                node.differentiation_level = DifferentiationLevel(payload["to_level"])
        elif event_type in {
            "node_started",
            "node_idled",
            "node_suspended",
            "node_resumed",
            "node_completed",
            "node_terminated",
            "node_failed",
        }:
            node = self.nodes.get(payload.get("node_id"))
            if node is not None:
                node.status = self._status_for_event(event_type, payload)

    def _append_child_once(self, parent_id: str, child_id: str) -> None:
        children = self.nodes[parent_id].child_ids
        if child_id not in children:
            children.append(child_id)

    @staticmethod
    def _status_for_event(event_type: str, payload: dict[str, Any]) -> Any:
        from vsm.nodes import NodeStatus

        if "status" in payload:
            return NodeStatus(payload["status"])
        event_status = {
            "node_started": NodeStatus.RUNNING,
            "node_idled": NodeStatus.IDLE,
            "node_suspended": NodeStatus.SUSPENDED,
            "node_resumed": NodeStatus.RUNNING,
            "node_completed": NodeStatus.COMPLETED,
            "node_terminated": NodeStatus.TERMINATED,
            "node_failed": NodeStatus.FAILED,
        }
        return event_status[event_type]
