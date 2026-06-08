"""Temporary Agent abstraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentSpec:
    model_spec: str
    system_prompt: str = ""
    tools: tuple[str, ...] = ()
    budget: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentInvocation:
    invocation_id: str
    node_id: str
    role_id: str
    agent_spec: AgentSpec
    context_view_ref: str | None = None


@dataclass(frozen=True)
class HumanAgent:
    human_id: str
    display_name: str
