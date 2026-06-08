"""Node lifecycle transition rules."""

from __future__ import annotations

from vsm.nodes.model import NodeStatus


NODE_STATUS_TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.CREATED: frozenset({NodeStatus.RUNNING, NodeStatus.TERMINATED, NodeStatus.FAILED}),
    NodeStatus.RUNNING: frozenset(
        {
            NodeStatus.IDLE,
            NodeStatus.WAITING_ESCALATION,
            NodeStatus.SUSPENDED,
            NodeStatus.COMPLETED,
            NodeStatus.TERMINATED,
            NodeStatus.FAILED,
        }
    ),
    NodeStatus.IDLE: frozenset({NodeStatus.RUNNING, NodeStatus.SUSPENDED, NodeStatus.TERMINATED, NodeStatus.FAILED}),
    NodeStatus.WAITING_ESCALATION: frozenset({NodeStatus.RUNNING, NodeStatus.SUSPENDED, NodeStatus.TERMINATED, NodeStatus.FAILED}),
    NodeStatus.SUSPENDED: frozenset({NodeStatus.RUNNING, NodeStatus.TERMINATED, NodeStatus.FAILED}),
    NodeStatus.COMPLETED: frozenset(),
    NodeStatus.TERMINATED: frozenset(),
    NodeStatus.FAILED: frozenset(),
}


def assert_transition_allowed(current: NodeStatus, target: NodeStatus) -> None:
    if target not in NODE_STATUS_TRANSITIONS[current]:
        raise ValueError(f"invalid Node lifecycle transition: {current.value} -> {target.value}")
