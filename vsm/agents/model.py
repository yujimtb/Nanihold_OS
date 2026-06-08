"""Temporary Agent abstraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class AgentSpec:
    model_spec: str
    system_prompt: str = ""
    tools: tuple[str, ...] = ()
    budget: dict[str, float] = field(default_factory=dict)
    spec_id: str | None = None
    spec_version: int = 1


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


@dataclass(frozen=True)
class PromptTemplate:
    spec_id: str
    spec_version: int
    body: str
    created_at: datetime
