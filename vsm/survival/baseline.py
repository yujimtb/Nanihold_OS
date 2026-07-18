"""Wave 0 の既存 VSM 構成基線。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from vsm.config import RunConfig
from vsm.roles import SystemRole


BASELINE_ROLE_COUNTS: Mapping[SystemRole, int] = {
    SystemRole.S1_WORKER: 0,
    SystemRole.S2_COORDINATOR: 1,
    SystemRole.S3_ALLOCATOR: 1,
    SystemRole.S3STAR_AUDITOR: 1,
    SystemRole.S4_SCANNER: 1,
    SystemRole.S5_POLICY: 1,
}


@dataclass(frozen=True, slots=True)
class BaselineSnapshot:
    schema_version: int
    role_counts: dict[str, int]
    mandatory_roles: tuple[str, ...]
    s1_hard_max: int
    s1_dynamic_max: int
    fixed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role_counts": dict(self.role_counts),
            "mandatory_roles": list(self.mandatory_roles),
            "s1_hard_max": self.s1_hard_max,
            "s1_dynamic_max": self.s1_dynamic_max,
            "fixed": self.fixed,
        }


def verify_baseline(config: RunConfig) -> BaselineSnapshot:
    """既存の起動基線を検証し、Dashboard に載せる snapshot を返す。"""

    actual = {role.value: config.count(role) for role in SystemRole}
    expected = {role.value: count for role, count in BASELINE_ROLE_COUNTS.items()}
    if actual != expected:
        raise ValueError(
            "Wave 0 baseline mismatch: "
            f"expected={expected!r}, actual={actual!r}"
        )
    if config.s1_max != 1024 or config.s1_dynamic_max != 64:
        raise ValueError(
            "Wave 0 S1 ceilings mismatch: expected s1_max=1024 and s1_dynamic_max=64"
        )
    return BaselineSnapshot(
        schema_version=1,
        role_counts=actual,
        mandatory_roles=tuple(
            role.value for role in SystemRole if role is not SystemRole.S1_WORKER
        ),
        s1_hard_max=config.s1_max,
        s1_dynamic_max=config.s1_dynamic_max,
    )
