"""Execution value object."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecutionStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Execution:
    """One processing unit where a Node runs an Agent or Tool in a Run."""

    execution_id: str
    run_id: str
    node_id: str
    agent_invocation_id: str | None = None
    tool_invocation_id: str | None = None
    status: ExecutionStatus = ExecutionStatus.CREATED

    def __post_init__(self) -> None:
        if self.agent_invocation_id is None and self.tool_invocation_id is None:
            raise ValueError("execution requires agent_invocation_id or tool_invocation_id")
