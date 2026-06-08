"""SQLite-backed adjacency-list graph projection."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    source_id: str
    target_id: str
    kind: str
    properties: dict[str, Any] = field(default_factory=dict)


class GraphProjection:
    """Minimal JSON-compatible adjacency list stored in SQLite."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialise(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS graph_nodes "
                "(id TEXT PRIMARY KEY, kind TEXT NOT NULL, properties_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS graph_edges "
                "(source_id TEXT NOT NULL, target_id TEXT NOT NULL, kind TEXT NOT NULL, "
                "properties_json TEXT NOT NULL, PRIMARY KEY(source_id, target_id, kind))"
            )

    def upsert_node(self, node: GraphNode) -> None:
        import json

        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO graph_nodes(id, kind, properties_json) VALUES(?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, properties_json=excluded.properties_json",
                (node.id, node.kind, json.dumps(node.properties, ensure_ascii=False, sort_keys=True)),
            )

    def upsert_edge(self, edge: GraphEdge) -> None:
        import json

        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO graph_edges(source_id, target_id, kind, properties_json) "
                "VALUES(?, ?, ?, ?)",
                (
                    edge.source_id,
                    edge.target_id,
                    edge.kind,
                    json.dumps(edge.properties, ensure_ascii=False, sort_keys=True),
                ),
            )
