"""Dynamic parent-issued authority/capability constraints."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from vsm.nodes.model import DifferentiationLevel
from vsm.tools.model import ToolEffect


@dataclass(frozen=True)
class ParentAuthority:
    authority_id: str
    issuer_node_id: str
    subject_node_id: str
    issued_at: datetime
    expires_at: datetime | None = None
    may_differentiate_to: DifferentiationLevel = DifferentiationLevel.COLLAPSED
    max_depth: int = 0
    max_spawn_count: int = 0
    budget_envelope: dict[str, float] = field(default_factory=dict)
    allowed_tool_classes: frozenset[ToolEffect] = field(default_factory=frozenset)
    denied_tool_classes: frozenset[ToolEffect] = field(default_factory=frozenset)
    data_scope: tuple[str, ...] = ()
    secret_scope: tuple[str, ...] = ()
    network_scope: tuple[str, ...] = ()
    filesystem_scope: tuple[str, ...] = ()
    termination_authority: bool = False
    escalation_contract: dict[str, str] = field(default_factory=dict)

    def allows_tool_effect(self, effect: ToolEffect) -> bool:
        if effect in self.denied_tool_classes:
            return False
        return not self.allowed_tool_classes or effect in self.allowed_tool_classes

    def assert_can_differentiate_to(self, target: DifferentiationLevel) -> None:
        order = [
            DifferentiationLevel.COLLAPSED,
            DifferentiationLevel.S5_ONLY,
            DifferentiationLevel.PARTIAL,
            DifferentiationLevel.FULL,
        ]
        if order.index(target) > order.index(self.may_differentiate_to):
            raise PermissionError(
                f"differentiation {target.value} exceeds authority {self.may_differentiate_to.value}"
            )


@dataclass(frozen=True)
class Lease:
    lease_id: str
    owner_node_id: str
    resource_ref: str
    lease_expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now >= self.lease_expires_at
