"""Algedonic signal tool facade."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from vsm.ids import generate_uuid
from vsm.messaging.message import SendResult
from vsm.tools.model import ToolEffect, ToolInvocation


@dataclass(frozen=True, slots=True)
class AlgedonicRequest:
    severity: str
    reason: str
    source_node_id: str

    def __post_init__(self) -> None:
        if self.severity not in {"pain", "pleasure"}:
            raise ValueError("severity must be pain or pleasure")
        if not self.reason.strip():
            raise ValueError("reason is required")
        if not self.source_node_id.strip():
            raise ValueError("source_node_id is required")


AlgedonicRunner = Callable[
    [AlgedonicRequest, ToolInvocation], Awaitable[SendResult]
]


@dataclass
class AlgedonicFacade:
    """Route an Algedonic signal through an injected platform runner."""

    runner: AlgedonicRunner

    async def raise_algedonic(
        self, request: AlgedonicRequest
    ) -> tuple[ToolInvocation, SendResult]:
        invocation_id = generate_uuid()
        invocation = ToolInvocation(
            invocation_id=invocation_id,
            tool_name="raise_algedonic",
            effect=ToolEffect.CONTROL,
            requested_by_node_id=request.source_node_id,
            payload={
                "severity": request.severity,
                "reason": request.reason,
                "source_node_id": request.source_node_id,
            },
            idempotency_key=invocation_id,
        )
        return invocation, await self.runner(request, invocation)


__all__ = ["AlgedonicFacade", "AlgedonicRequest", "AlgedonicRunner"]
