"""Proposal の主状態と直交 pause cause の状態機械。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Literal


class ProposalPhase(str, Enum):
    PROPOSED = "PROPOSED"
    CONSORTIUM_REVIEW = "CONSORTIUM_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    WORKSPACE_READY = "WORKSPACE_READY"
    IMPLEMENTING = "IMPLEMENTING"
    GATES_RUNNING = "GATES_RUNNING"
    GATES_PASSED = "GATES_PASSED"
    GATES_FAILED = "GATES_FAILED"
    ABORTED = "ABORTED"
    AUDIT = "AUDIT"
    FINAL_CONSORTIUM = "FINAL_CONSORTIUM"
    MERGE_READY = "MERGE_READY"
    REJECTED_FINAL = "REJECTED_FINAL"
    DONE = "DONE"
    ARCHIVED = "ARCHIVED"


class PauseKind(str, Enum):
    SUSPEND = "SUSPEND"
    QUOTA_WAIT = "QUOTA_WAIT"


TERMINAL_PHASES = frozenset(
    {
        ProposalPhase.REJECTED,
        ProposalPhase.ABORTED,
        ProposalPhase.REJECTED_FINAL,
        ProposalPhase.DONE,
        ProposalPhase.ARCHIVED,
    }
)

_TRANSITIONS: dict[ProposalPhase, frozenset[ProposalPhase]] = {
    ProposalPhase.PROPOSED: frozenset({ProposalPhase.CONSORTIUM_REVIEW}),
    ProposalPhase.CONSORTIUM_REVIEW: frozenset(
        {ProposalPhase.APPROVED, ProposalPhase.REJECTED, ProposalPhase.NEEDS_HUMAN, ProposalPhase.ABORTED}
    ),
    ProposalPhase.APPROVED: frozenset({ProposalPhase.WORKSPACE_READY, ProposalPhase.ABORTED}),
    ProposalPhase.NEEDS_HUMAN: frozenset(
        {ProposalPhase.APPROVED, ProposalPhase.REJECTED, ProposalPhase.CONSORTIUM_REVIEW, ProposalPhase.ABORTED}
    ),
    ProposalPhase.WORKSPACE_READY: frozenset({ProposalPhase.IMPLEMENTING, ProposalPhase.ABORTED}),
    ProposalPhase.IMPLEMENTING: frozenset({ProposalPhase.GATES_RUNNING, ProposalPhase.ABORTED}),
    ProposalPhase.GATES_RUNNING: frozenset(
        {ProposalPhase.GATES_PASSED, ProposalPhase.GATES_FAILED, ProposalPhase.ABORTED}
    ),
    ProposalPhase.GATES_FAILED: frozenset({ProposalPhase.GATES_RUNNING, ProposalPhase.ABORTED}),
    ProposalPhase.GATES_PASSED: frozenset({ProposalPhase.AUDIT, ProposalPhase.ABORTED}),
    ProposalPhase.AUDIT: frozenset({ProposalPhase.FINAL_CONSORTIUM, ProposalPhase.ABORTED}),
    ProposalPhase.FINAL_CONSORTIUM: frozenset(
        {ProposalPhase.MERGE_READY, ProposalPhase.REJECTED_FINAL, ProposalPhase.ABORTED}
    ),
    ProposalPhase.MERGE_READY: frozenset({ProposalPhase.DONE, ProposalPhase.ARCHIVED, ProposalPhase.ABORTED}),
    ProposalPhase.REJECTED: frozenset(),
    ProposalPhase.ABORTED: frozenset(),
    ProposalPhase.REJECTED_FINAL: frozenset(),
    ProposalPhase.DONE: frozenset(),
    ProposalPhase.ARCHIVED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class PauseCause:
    pause_id: str
    kind: PauseKind
    actor_type: Literal["human", "node", "controller"]
    actor_id: str
    pool_id: str | None = None
    reset_at: datetime | None = None
    source_event_id: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.kind, str):
            object.__setattr__(self, "kind", PauseKind(self.kind))
        if isinstance(self.reset_at, str):
            object.__setattr__(
                self,
                "reset_at",
                datetime.fromisoformat(self.reset_at.replace("Z", "+00:00")),
            )
        if not self.pause_id or not self.actor_id or not self.source_event_id or not self.reason:
            raise ValueError("PauseCause の識別子・source_event_id・reason は必須です")
        if self.kind is PauseKind.SUSPEND and (self.pool_id is not None or self.reset_at is not None):
            raise ValueError("SUSPEND pause に pool_id/reset_at は指定できません")
        if self.kind is PauseKind.QUOTA_WAIT and (not self.pool_id or self.reset_at is None):
            raise ValueError("QUOTA_WAIT pause には pool_id/reset_at が必要です")
        if self.reset_at is not None:
            if self.reset_at.tzinfo is None:
                raise ValueError("pause reset_at は timezone-aware でなければなりません")
            object.__setattr__(self, "reset_at", self.reset_at.astimezone(timezone.utc))


@dataclass(frozen=True, slots=True)
class ProposalAggregate:
    phase: ProposalPhase = ProposalPhase.PROPOSED
    pause_causes: tuple[PauseCause, ...] = ()
    state_version: int = 1
    active_run_id: str | None = None
    implementation_run_ids: tuple[str, ...] = ()
    repair_used: bool = False
    gate_attempt: Literal[0, 1, 2] = 0

    def __post_init__(self) -> None:
        if isinstance(self.phase, str):
            object.__setattr__(self, "phase", ProposalPhase(self.phase))
        object.__setattr__(self, "pause_causes", tuple(self.pause_causes))
        object.__setattr__(self, "implementation_run_ids", tuple(self.implementation_run_ids))
        if self.state_version < 1:
            raise ValueError("state_version は1以上でなければなりません")
        if len({pause.pause_id for pause in self.pause_causes}) != len(self.pause_causes):
            raise ValueError("pause_id は unique でなければなりません")
        if len(set(self.implementation_run_ids)) != len(self.implementation_run_ids):
            raise ValueError("implementation_run_ids は unique でなければなりません")

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def is_paused(self) -> bool:
        return bool(self.pause_causes)

    def pause(self, cause: PauseCause) -> "ProposalAggregate":
        if self.is_terminal:
            raise ValueError("terminal Proposal は pause できません")
        if any(item.pause_id == cause.pause_id for item in self.pause_causes):
            raise ValueError(f"pause_id は既に存在します: {cause.pause_id}")
        return replace(
            self,
            pause_causes=(*self.pause_causes, cause),
            state_version=self.state_version + 1,
        )

    def resume(self, pause_id: str) -> "ProposalAggregate":
        if not pause_id:
            raise ValueError("pause_id は必須です")
        remaining = tuple(item for item in self.pause_causes if item.pause_id != pause_id)
        if len(remaining) == len(self.pause_causes):
            raise ValueError(f"存在しない pause_id です: {pause_id}")
        return replace(self, pause_causes=remaining, state_version=self.state_version + 1)


def assert_transition_allowed(current: ProposalPhase, target: ProposalPhase) -> None:
    if current in TERMINAL_PHASES:
        raise ValueError(f"terminal Proposal は遷移できません: {current.value} -> {target.value}")
    if target not in _TRANSITIONS[current]:
        raise ValueError(f"許可されていない Proposal 遷移です: {current.value} -> {target.value}")


class ProposalStateMachine:
    """reducer に渡す aggregate mutation を一箇所へ集約する。"""

    def __init__(self, aggregate: ProposalAggregate | None = None) -> None:
        self.aggregate = aggregate or ProposalAggregate()

    def transition(
        self,
        target: ProposalPhase,
        *,
        allow_while_paused: bool = False,
        gate_attempt: Literal[0, 1, 2] | None = None,
        active_run_id: str | None = None,
    ) -> ProposalAggregate:
        current = self.aggregate
        if isinstance(target, str):
            target = ProposalPhase(target)
        assert_transition_allowed(current.phase, target)
        if current.pause_causes and target is not ProposalPhase.ABORTED and not allow_while_paused:
            raise ValueError("pause_causes が存在する間は主状態を前進できません")
        repair_used = current.repair_used or (
            current.phase is ProposalPhase.GATES_FAILED and target is ProposalPhase.GATES_RUNNING
        )
        next_attempt = gate_attempt if gate_attempt is not None else current.gate_attempt
        if target is ProposalPhase.GATES_RUNNING and current.phase is ProposalPhase.IMPLEMENTING:
            next_attempt = max(next_attempt, 1)
        if target is ProposalPhase.GATES_RUNNING and current.phase is ProposalPhase.GATES_FAILED:
            next_attempt = 2
        run_ids = current.implementation_run_ids
        if active_run_id is not None and active_run_id not in run_ids:
            run_ids = (*run_ids, active_run_id)
        self.aggregate = replace(
            current,
            phase=target,
            state_version=current.state_version + 1,
            repair_used=repair_used,
            gate_attempt=next_attempt,
            active_run_id=active_run_id if active_run_id is not None else current.active_run_id,
            implementation_run_ids=run_ids,
        )
        return self.aggregate

    def add_pause(self, cause: PauseCause) -> ProposalAggregate:
        self.aggregate = self.aggregate.pause(cause)
        return self.aggregate

    def remove_pause(self, pause_id: str) -> ProposalAggregate:
        self.aggregate = self.aggregate.resume(pause_id)
        return self.aggregate


def transition_table() -> dict[ProposalPhase, frozenset[ProposalPhase]]:
    return dict(_TRANSITIONS)


def is_terminal(phase: ProposalPhase) -> bool:
    return phase in TERMINAL_PHASES


__all__ = [
    "PauseCause",
    "PauseKind",
    "ProposalAggregate",
    "ProposalPhase",
    "ProposalStateMachine",
    "TERMINAL_PHASES",
    "assert_transition_allowed",
    "is_terminal",
    "transition_table",
]
