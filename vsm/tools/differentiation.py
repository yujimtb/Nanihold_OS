"""Differentiation facade tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vsm.ids import generate_uuid
from vsm.nodes import DifferentiationLevel
from vsm.tools.model import ToolEffect, ToolInvocation

if TYPE_CHECKING:
    from vsm.authority import ParentAuthority


@dataclass(frozen=True)
class DifferentiationRequest:
    differentiation_key: str
    node_id: str
    requested_by: str
    target_level: DifferentiationLevel


@dataclass
class DifferentiationFacade:
    """Control facade that enforces ParentAuthority before differentiation."""

    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def differentiate(
        self,
        request: DifferentiationRequest,
        authority: ParentAuthority,
    ) -> ToolInvocation:
        authority.assert_can_differentiate_to(request.target_level)
        if request.differentiation_key in self.decisions:
            result = dict(self.decisions[request.differentiation_key])
        else:
            result = {
                "status": "requested",
                "node_id": request.node_id,
                "to_level": request.target_level.value,
            }
            self.decisions[request.differentiation_key] = result

        return ToolInvocation(
            invocation_id=generate_uuid(),
            tool_name="differentiate",
            effect=ToolEffect.CONTROL,
            requested_by_node_id=request.requested_by,
            payload={
                "differentiation_key": request.differentiation_key,
                "node_id": request.node_id,
                "to_level": request.target_level.value,
                "result": result,
            },
            idempotency_key=request.differentiation_key,
        )
