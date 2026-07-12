"""Agent-layer specs."""

from vsm.agents.model import AgentInvocation, AgentSpec, HumanAgent, PromptTemplate
from vsm.agents.runtime import (
    AgentRequest,
    AgentResult,
    AgentRuntimeError,
    AgentRuntimeProtocol,
)

__all__ = [
    "AgentInvocation",
    "AgentRequest",
    "AgentResult",
    "AgentRuntimeError",
    "AgentRuntimeProtocol",
    "AgentSpec",
    "HumanAgent",
    "PromptTemplate",
]
