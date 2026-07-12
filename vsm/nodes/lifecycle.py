"""Node lifecycle transition rules."""

from __future__ import annotations

from vsm.nodes.model import Node, NodeRunState, NodeStatus


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


def transition_node_status(
    node: Node,
    run_state: NodeRunState,
    target: NodeStatus,
) -> None:
    """Node と Run 状態を検証付きの単一操作で同時に遷移させる。"""

    if node.id != run_state.node_id:
        raise ValueError(
            f"Node lifecycle target mismatch: {node.id} != {run_state.node_id}"
        )
    if node.status is not run_state.status:
        raise ValueError(
            "Node lifecycle state mismatch: "
            f"node={node.status.value}, run_state={run_state.status.value}"
        )
    assert_transition_allowed(node.status, target)
    node.status = target
    run_state.status = target
