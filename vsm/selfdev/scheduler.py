"""自己開発 ready-queue の直列 admission。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Mapping

from vsm.selfdev.models import ProposalManifest
from vsm.selfdev.ready_queue import AdmissionResult, dependencies_satisfied, quota_admissible, scope_overlaps


@dataclass(frozen=True, slots=True)
class SchedulerDecision:
    proposal: ProposalManifest | None
    admission: AdmissionResult


@dataclass
class SelfDevScheduler:
    """候補集合から同時1件だけを選ぶ scheduler。"""

    candidates: list[ProposalManifest] = field(default_factory=list)
    multiplier: float = 1.3
    runtime_guard: Callable[[ProposalManifest], bool] | None = None

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            raise ValueError("scheduler multiplier は正数でなければなりません")
        initial = tuple(self.candidates)
        self.candidates = []
        self._by_id: dict[str, ProposalManifest] = {}
        for candidate in initial:
            self.add(candidate)

    def add(self, proposal: ProposalManifest) -> None:
        if proposal.id in self._by_id:
            raise ValueError(f"ready-queue に同じ Proposal が存在します: {proposal.id}")
        self._by_id[proposal.id] = proposal
        self.candidates.append(proposal)

    def remove(self, proposal_id: str) -> None:
        if proposal_id not in self._by_id:
            raise KeyError(proposal_id)
        del self._by_id[proposal_id]
        self.candidates[:] = [candidate for candidate in self.candidates if candidate.id != proposal_id]

    def decide(
        self,
        *,
        active: ProposalManifest | None,
        done_ids: Iterable[str],
        remaining: Mapping[str, float],
        reserve: Mapping[str, float],
        merge_ready: Iterable[ProposalManifest] = (),
        protected_approved_ids: Iterable[str] = (),
        paused_ids: Iterable[str] = (),
    ) -> SchedulerDecision:
        if active is not None:
            return SchedulerDecision(None, AdmissionResult(False, "active Proposal slot は既に使用中です"))
        done = tuple(done_ids)
        conflicts = tuple(merge_ready)
        protected_approved = set(protected_approved_ids)
        paused = set(paused_ids)
        ordered = sorted(self.candidates, key=lambda item: (item.created_at, item.id))
        first_reason: str | None = None
        for candidate in ordered:
            if candidate.id in paused:
                first_reason = first_reason or "Proposal が pause 中です"
                continue
            if not dependencies_satisfied(candidate, done):
                first_reason = first_reason or "依存 Proposal が DONE ではありません"
                continue
            if any(scope_overlaps(candidate, merged) for merged in conflicts):
                first_reason = first_reason or "MERGE_READY 候補と scope が競合しています"
                continue
            if candidate.risk_class == "protected" and candidate.id not in protected_approved:
                first_reason = first_reason or "protected Proposal の事前 Human approval がありません"
                continue
            if self.runtime_guard is not None and not self.runtime_guard(candidate):
                first_reason = first_reason or "runtime/backend/model 設定が要件と不一致です"
                continue
            quota = quota_admissible(
                candidate,
                remaining=remaining,
                reserve=reserve,
                multiplier=self.multiplier,
            )
            if not quota.admissible:
                first_reason = first_reason or quota.reason
                continue
            return SchedulerDecision(candidate, AdmissionResult(True))
        return SchedulerDecision(None, AdmissionResult(False, first_reason or "開始可能な候補がありません"))

    async def start_next(
        self,
        *,
        submit: Callable[[ProposalManifest], Awaitable[object]],
        active: ProposalManifest | None,
        done_ids: Iterable[str],
        remaining: Mapping[str, float],
        reserve: Mapping[str, float],
        merge_ready: Iterable[ProposalManifest] = (),
        protected_approved_ids: Iterable[str] = (),
        paused_ids: Iterable[str] = (),
    ) -> ProposalManifest | None:
        decision = self.decide(
            active=active,
            done_ids=done_ids,
            remaining=remaining,
            reserve=reserve,
            merge_ready=merge_ready,
            protected_approved_ids=protected_approved_ids,
            paused_ids=paused_ids,
        )
        if decision.proposal is None:
            return None
        proposal = decision.proposal
        await submit(proposal)
        self.remove(proposal.id)
        return proposal


ReadyQueueScheduler = SelfDevScheduler

__all__ = ["ReadyQueueScheduler", "SchedulerDecision", "SelfDevScheduler"]
