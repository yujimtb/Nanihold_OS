"""Node lifecycle control tool facades."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vsm.ids import generate_uuid
from vsm.nodes import Node, NodeStatus, assert_transition_allowed
from vsm.tools.model import ToolEffect, ToolInvocation

if TYPE_CHECKING:
    from vsm.authority import ParentAuthority


@dataclass(frozen=True)
class NodeControlRequest:
    control_key: str
    requested_by: str
    node: Node
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.control_key.strip():
            raise ValueError("control_key is required")
        if not self.requested_by.strip():
            raise ValueError("requested_by is required")


@dataclass
class NodeControlFacade:
    """Control facade for suspend, resume and terminate node actions."""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)

    def suspend_node(
        self,
        request: NodeControlRequest,
        authority: ParentAuthority,
    ) -> ToolInvocation:
        return self._control("suspend_node", NodeStatus.SUSPENDED, request, authority)

    def resume_node(
        self,
        request: NodeControlRequest,
        authority: ParentAuthority,
    ) -> ToolInvocation:
        return self._control("resume_node", NodeStatus.RUNNING, request, authority)

    def terminate_node(
        self,
        request: NodeControlRequest,
        authority: ParentAuthority,
    ) -> ToolInvocation:
        if not authority.termination_authority:
            raise PermissionError("terminate_node requires termination_authority")
        if not request.node.terminable:
            raise PermissionError("terminate_node requires a terminable node")
        return self._control("terminate_node", NodeStatus.TERMINATED, request, authority)

    def _control(
        self,
        tool_name: str,
        target_status: NodeStatus,
        request: NodeControlRequest,
        authority: ParentAuthority,
    ) -> ToolInvocation:
        if not authority.allows_tool_effect(ToolEffect.CONTROL):
            raise PermissionError(f"{tool_name} effect is denied by authority: CONTROL")
        if request.control_key in self.results:
            result = dict(self.results[request.control_key])
        else:
            assert_transition_allowed(request.node.status, target_status)
            request.node.status = target_status
            result = {
                "node_id": request.node.id,
                "status": target_status.value,
                "reason": request.reason,
            }
            self.results[request.control_key] = result

        return ToolInvocation(
            invocation_id=generate_uuid(),
            tool_name=tool_name,
            effect=ToolEffect.CONTROL,
            requested_by_node_id=request.requested_by,
            payload={
                "control_key": request.control_key,
                "node_id": request.node.id,
                "target_status": target_status.value,
                "reason": request.reason,
                "result": result,
            },
            idempotency_key=request.control_key,
        )
