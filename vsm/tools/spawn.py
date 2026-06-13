"""Child spawning tool facade."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vsm.ids import generate_uuid
from vsm.tools.model import ToolEffect, ToolInvocation

if TYPE_CHECKING:
    from vsm.authority import ParentAuthority


@dataclass(frozen=True)
class SpawnChildRequest:
    spawn_key: str
    requested_by: str
    specialization: str
    initial_assignment: dict[str, Any] | str

    def __post_init__(self) -> None:
        if not self.spawn_key.strip():
            raise ValueError("spawn_key is required")
        if not self.requested_by.strip():
            raise ValueError("requested_by is required")
        if not self.specialization.strip():
            raise ValueError("specialization is required")


@dataclass(frozen=True)
class SpawnChildResult:
    node_id: str

    def to_payload(self) -> dict[str, Any]:
        return {"node_id": self.node_id}


SpawnChildRunner = Callable[[SpawnChildRequest, ToolInvocation], Awaitable[SpawnChildResult]]


@dataclass
class SpawnChildFacade:
    """Spawn a child through a caller-provided runtime runner."""

    runner: SpawnChildRunner
    results: dict[str, SpawnChildResult] = field(default_factory=dict)

    async def spawn_child(
        self,
        request: SpawnChildRequest,
        authority: ParentAuthority,
    ) -> tuple[ToolInvocation, SpawnChildResult]:
        if not authority.allows_tool_effect(ToolEffect.CONTROL):
            raise PermissionError("spawn_child effect is denied by authority: CONTROL")
        if authority.max_spawn_count > 0 and request.spawn_key not in self.results:
            if len(self.results) >= authority.max_spawn_count:
                raise PermissionError("spawn_child exceeds authority max_spawn_count")

        invocation = ToolInvocation(
            invocation_id=generate_uuid(),
            tool_name="spawn_child",
            effect=ToolEffect.CONTROL,
            requested_by_node_id=request.requested_by,
            payload={
                "spawn_key": request.spawn_key,
                "specialization": request.specialization,
                "initial_assignment": request.initial_assignment,
            },
            idempotency_key=request.spawn_key,
        )
        if request.spawn_key in self.results:
            return invocation, self.results[request.spawn_key]

        result = await self.runner(request, invocation)
        self.results[request.spawn_key] = result
        return invocation, result
