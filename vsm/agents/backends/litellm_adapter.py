"""既存 LLMProviderProtocol を AgentRuntime へ接続するアダプタ。"""

from __future__ import annotations

from dataclasses import dataclass

from vsm.agents.runtime import AgentRequest, AgentResult
from vsm.llm.types import LLMProviderProtocol


@dataclass
class LiteLLMRuntimeAdapter:
    """既存プロバイダーの応答を AgentResult に変換する。"""

    provider: LLMProviderProtocol
    timeout_seconds: float = 60.0
    backend_name: str = "litellm"

    async def invoke(self, request: AgentRequest) -> AgentResult:
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
