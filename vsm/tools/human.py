"""Human review request tool facade."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vsm.agents import HumanAgent
from vsm.ids import generate_uuid
from vsm.tools.model import ToolEffect, ToolInvocation

if TYPE_CHECKING:
    from vsm.authority import ParentAuthority


@dataclass(frozen=True)
class HumanReviewRequest:
    review_key: str
    requested_by: str
    reason: str
    subject: str
    human: HumanAgent | None = None

    def __post_init__(self) -> None:
        if not self.review_key.strip():
            raise ValueError("review_key is required")
        if not self.requested_by.strip():
            raise ValueError("requested_by is required")
        if not self.reason.strip():
            raise ValueError("reason is required")
        if not self.subject.strip():
            raise ValueError("subject is required")


@dataclass
class HumanReviewFacade:
    """Idempotent facade for requesting human review."""

    requests: dict[str, dict[str, Any]] = field(default_factory=dict)

    def request_human_review(
        self,
        request: HumanReviewRequest,
        authority: ParentAuthority | None = None,
    ) -> ToolInvocation:
        if authority is not None and not authority.allows_tool_effect(ToolEffect.HUMAN):
            raise PermissionError("request_human_review effect is denied by authority: HUMAN")
        if request.review_key in self.requests:
            result = dict(self.requests[request.review_key])
        else:
            result = {
                "status": "requested",
                "reason": request.reason,
                "subject": request.subject,
                "human_id": request.human.human_id if request.human else None,
            }
            self.requests[request.review_key] = result

        return ToolInvocation(
            invocation_id=generate_uuid(),
            tool_name="request_human_review",
            effect=ToolEffect.HUMAN,
            requested_by_node_id=request.requested_by,
            payload={
                "review_key": request.review_key,
                "reason": request.reason,
                "subject": request.subject,
                "human": (
                    {
                        "human_id": request.human.human_id,
                        "display_name": request.human.display_name,
                    }
                    if request.human
                    else None
                ),
                "result": result,
            },
        )
