"""S2 coordination facade tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vsm.ids import generate_uuid
from vsm.tools.model import ToolEffect, ToolInvocation


@dataclass(frozen=True)
class CoordinationRequest:
    coordination_key: str
    scope: str
    participants: tuple[str, ...]
    issue: str
    requested_by: str


@dataclass
class CoordinationFacade:
    """Idempotent facade that records coordination requests before S2 decides."""

    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def request_coordination(self, request: CoordinationRequest) -> ToolInvocation:
        if request.coordination_key in self.decisions:
            result = dict(self.decisions[request.coordination_key])
        else:
            result = {"status": "requested", "issue": request.issue}
            self.decisions[request.coordination_key] = result
        return ToolInvocation(
            invocation_id=generate_uuid(),
            tool_name="request_coordination",
            effect=ToolEffect.CONTROL,
            requested_by_node_id=request.requested_by,
            payload={
                "coordination_key": request.coordination_key,
                "scope": request.scope,
                "participants": list(request.participants),
                "issue": request.issue,
                "result": result,
            },
            idempotency_key=request.coordination_key,
        )
