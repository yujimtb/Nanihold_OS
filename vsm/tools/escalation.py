"""Escalation facade tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vsm.ids import generate_uuid
from vsm.tools.model import ToolEffect, ToolInvocation


@dataclass(frozen=True)
class EscalationRequest:
    escalation_key: str
    reason: str
    blocking_issue: str
    requested_by: str
    target_authority: str


@dataclass
class EscalationFacade:
    """Idempotent facade for parent or authority escalation requests."""

    requests: dict[str, dict[str, Any]] = field(default_factory=dict)

    def request_escalation(self, request: EscalationRequest) -> ToolInvocation:
        if request.escalation_key in self.requests:
            result = dict(self.requests[request.escalation_key])
        else:
            result = {
                "status": "requested",
                "reason": request.reason,
                "blocking_issue": request.blocking_issue,
                "target_authority": request.target_authority,
            }
            self.requests[request.escalation_key] = result

        return ToolInvocation(
            invocation_id=generate_uuid(),
            tool_name="request_escalation",
            effect=ToolEffect.CONTROL,
            requested_by_node_id=request.requested_by,
            payload={
                "escalation_key": request.escalation_key,
                "reason": request.reason,
                "blocking_issue": request.blocking_issue,
                "target_authority": request.target_authority,
                "result": result,
            },
            idempotency_key=request.escalation_key,
        )
