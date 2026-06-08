"""Tool contracts used by Role and ParentAuthority."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ToolEffect(str, Enum):
    PURE_READ = "PURE_READ"
    LOCAL_WRITE = "LOCAL_WRITE"
    EXTERNAL_READ = "EXTERNAL_READ"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"
    CONTROL = "CONTROL"
    HUMAN = "HUMAN"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    effect: ToolEffect
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    requires_idempotency_key: bool | None = None

    def __post_init__(self) -> None:
        required = self.effect in {ToolEffect.EXTERNAL_WRITE, ToolEffect.CONTROL}
        if self.requires_idempotency_key is None:
            object.__setattr__(self, "requires_idempotency_key", required)


@dataclass(frozen=True)
class ToolInvocation:
    invocation_id: str
    tool_name: str
    effect: ToolEffect
    requested_by_node_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.effect in {ToolEffect.EXTERNAL_WRITE, ToolEffect.CONTROL} and not self.idempotency_key:
            raise ValueError(f"{self.effect.value} tool invocations require idempotency_key")
