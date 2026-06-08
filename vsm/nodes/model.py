"""Single Node abstraction for static and dynamic VSM units."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vsm.roles import RoleSpec, SystemRole


class NodeStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    IDLE = "IDLE"
    WAITING_ESCALATION = "WAITING_ESCALATION"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    TERMINATED = "TERMINATED"
    FAILED = "FAILED"


class DifferentiationLevel(str, Enum):
    COLLAPSED = "COLLAPSED"
    S5_ONLY = "S5_ONLY"
    PARTIAL = "PARTIAL"
    FULL = "FULL"


@dataclass
class Node:
    """Persistent unit that owns responsibility, history, authority and state."""

    id: str
    parent_id: str | None
    vsm_position: SystemRole | str
    goal: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    termination_condition: str | None = None
    terminable: bool = True
    differentiation_level: DifferentiationLevel = DifferentiationLevel.COLLAPSED
    predefined_children: tuple[str, ...] = ()
    role_spec: RoleSpec | None = None
    agent_spec: dict[str, Any] = field(default_factory=dict)
    parent_authority: str | None = None
    child_ids: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    summary_refs: list[str] = field(default_factory=list)
    status: NodeStatus = NodeStatus.CREATED
    output: dict[str, Any] = field(default_factory=dict)

    @property
    def is_static(self) -> bool:
        return not self.terminable and self.termination_condition is None


@dataclass
class NodeRunState:
    """Run-specific state for one Node."""

    run_id: str
    node_id: str
    status: NodeStatus = NodeStatus.CREATED
    budget: dict[str, float] = field(default_factory=dict)
    cost_consumed: dict[str, float] = field(default_factory=dict)
    context_view_ref: str | None = None
    output_ref: str | None = None
