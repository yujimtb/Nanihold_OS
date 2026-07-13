"""決定論テスト用 AgentRuntime。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vsm.agents.runtime import AgentRequest, AgentResult, AgentRuntimeError
from vsm.llm.types import LLMProviderProtocol

FakeResponse = str | Callable[[AgentRequest], AgentResult | str]


@dataclass
class FakeAgentRuntime:
    """応答、遅延、エラーを明示的に制御できるフェイク。"""

    response: FakeResponse = "ok"
    latency: float = 0.0
    error: AgentRuntimeError | None = None
    model: str = "fake/test-model"
    tokens_in: int = 1
    tokens_out: int = 1
    tokens_cache_read: int = 0
    session_ref: str | None = None
    quota_pool: str | None = None
    provider: LLMProviderProtocol | None = None
    timeout_seconds: float = 60.0
    invocations: list[AgentRequest] = field(default_factory=list)

    backend_name = "fake"

    async def invoke(self, request: AgentRequest) -> AgentResult:
        self.invocations.append(request)
        if self.provider is not None:
            prompt = request.prompt
            if request.context_view:
                prompt = f"{request.context_view}\n\n{prompt}"
            response = await self.provider.invoke(prompt, model=request.model)
            return AgentResult(
                text=response.text,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                tokens_cache_read=0,
                latency_ms=response.latency_ms,
                model=response.model,
                backend=self.backend_name,
                session_ref=None,
            )
        if self.error is not None:
            if self.latency:
                await asyncio.sleep(self.latency)
            raise self.error
        if callable(self.response):
            value = self.response(request)
            if isinstance(value, AgentResult):
                result = value
            else:
                result = str(value)
        else:
            result = self.response
        if self.latency:
            await asyncio.sleep(self.latency)
        if isinstance(result, AgentResult):
            return result
        return AgentResult(
            text=str(result),
            tokens_in=self.tokens_in,
            tokens_out=self.tokens_out,
            tokens_cache_read=self.tokens_cache_read,
            latency_ms=int(self.latency * 1000),
            model=request.model or self.model,
            backend=self.backend_name,
            session_ref=self.session_ref,
        )
