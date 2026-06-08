"""VSM role identifiers and role contract specs.

This package keeps the historical ``vsm.roles`` import surface while adding
the RoleSpec layer described by ``refactor_20260608.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = ["SystemRole", "MANDATORY_ROLES", "RoleSpec"]


class SystemRole(str, Enum):
    """Identifier for each VSM position represented by the PoC platform."""

    S1_WORKER = "S1_WORKER"
    S2_COORDINATOR = "S2_COORDINATOR"
    S3_ALLOCATOR = "S3_ALLOCATOR"
    S3STAR_AUDITOR = "S3STAR_AUDITOR"
    S4_SCANNER = "S4_SCANNER"
    S5_POLICY = "S5_POLICY"


MANDATORY_ROLES: frozenset[SystemRole] = frozenset(
    {
        SystemRole.S2_COORDINATOR,
        SystemRole.S3_ALLOCATOR,
        SystemRole.S3STAR_AUDITOR,
        SystemRole.S4_SCANNER,
        SystemRole.S5_POLICY,
    }
)


@dataclass(frozen=True)
class RoleSpec:
    """Static contract for a Node's VSM responsibility.

    Escalation destinations and dynamic permission boundaries are intentionally
    absent; those are injected through ParentAuthority at runtime.
    """

    id: str
    vsm_position: SystemRole | str
    responsibility: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    escalation_contract: dict[str, Any] = field(default_factory=dict)
    prompt_template: str = ""
