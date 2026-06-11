"""LLM call tool facade."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vsm.ids import generate_uuid
from vsm.llm.types import LLMProviderProtocol, LLMResponse
from vsm.tools.model import ToolEffect, ToolInvocation

if TYPE_CHECKING:
    from vsm.authority import ParentAuthority


@dataclass(frozen=True)
class LLMCallRequest:
    prompt: str
    requested_by: str
    model: str | None = None
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("prompt is required")
        if not self.requested_by.strip():
            raise ValueError("requested_by is required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class LLMCallResult:
    response: LLMResponse

    def to_payload(self) -> dict[str, Any]:
        return {
            "model": self.response.model,
            "response": self.response.text,
            "latency_ms": self.response.latency_ms,
            "tokens_in": self.response.tokens_in,
            "tokens_out": self.response.tokens_out,
        }


class LLMCallFacade:
    """Execute an LLM provider call as an ``llm_call`` ToolInvocation."""

    async def call(
        self,
        request: LLMCallRequest,
        authority: ParentAuthority,
        provider: LLMProviderProtocol,
    ) -> tuple[ToolInvocation, LLMCallResult]:
        if not authority.allows_tool_effect(ToolEffect.EXTERNAL_READ):
            raise PermissionError("llm_call effect is denied by authority: EXTERNAL_READ")

        invocation = ToolInvocation(
            invocation_id=generate_uuid(),
            tool_name="llm_call",
            effect=ToolEffect.EXTERNAL_READ,
            requested_by_node_id=request.requested_by,
            payload={
                "prompt": request.prompt,
                "model": request.model,
                "timeout_seconds": request.timeout_seconds,
            },
        )
        response = await asyncio.wait_for(
            provider.invoke(request.prompt, model=request.model),
            timeout=request.timeout_seconds,
        )
        return invocation, LLMCallResult(response=response)
