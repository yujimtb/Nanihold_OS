"""Static seed topology and live topology projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vsm.nodes import DifferentiationLevel, Node
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
            predefined_children=self.delegates_to,
        )


@dataclass
class LiveTopology:
    nodes: dict[str, Node] = field(default_factory=dict)

    @classmethod
    def from_static(cls, entries: list[StaticTopologyEntry]) -> "LiveTopology":
        topology = cls()
        for entry in entries:
            node = entry.to_node()
            topology.nodes[node.id] = node
            if node.parent_id and node.parent_id in topology.nodes:
                topology.nodes[node.parent_id].child_ids.append(node.id)
        return topology

    def apply_event(self, event: dict[str, Any]) -> None:
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
            )
            self.nodes[node.id] = node
            if node.parent_id and node.parent_id in self.nodes:
                self.nodes[node.parent_id].child_ids.append(node.id)
        elif event_type == "node_differentiated":
            node = self.nodes.get(payload.get("node_id"))
            if node is not None:
                node.differentiation_level = DifferentiationLevel(payload["to_level"])
