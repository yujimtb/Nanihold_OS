"""ready-queue の純粋な dependency/scope/quota admission 判定。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from vsm.selfdev.models import ProposalManifest, path_in_scope


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    admissible: bool
    reason: str | None = None


def scope_overlaps(left: ProposalManifest, right: ProposalManifest) -> bool:
    for left_rule in left.scope:
        for right_rule in right.scope:
            if path_in_scope(left_rule.path, (right_rule,)) or path_in_scope(right_rule.path, (left_rule,)):
                return True
    return False


def dependencies_satisfied(proposal: ProposalManifest, done_ids: Iterable[str]) -> bool:
    return set(proposal.dependencies).issubset(set(done_ids))


def dependency_cycle(proposals: Iterable[ProposalManifest]) -> tuple[str, ...] | None:
    """依存グラフの最初の cycle を返し、推測による選定を防ぐ。"""

    by_id = {proposal.id: proposal for proposal in proposals}
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(proposal_id: str) -> tuple[str, ...] | None:
        if proposal_id in visiting:
            index = visiting.index(proposal_id)
            return tuple((*visiting[index:], proposal_id))
        if proposal_id in visited:
            return None
        proposal = by_id.get(proposal_id)
        if proposal is None:
            return None
        visiting.append(proposal_id)
        for dependency in proposal.dependencies:
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        visiting.pop()
        visited.add(proposal_id)
        return None

    for proposal_id in by_id:
        cycle = visit(proposal_id)
        if cycle is not None:
            return cycle
    return None


def validate_dependency_graph(proposals: Iterable[ProposalManifest]) -> None:
    proposals = tuple(proposals)
    ids = {proposal.id for proposal in proposals}
    missing = sorted(
        dependency
        for proposal in proposals
        for dependency in proposal.dependencies
        if dependency not in ids
    )
    if missing:
        raise ValueError(f"依存 Proposal が候補集合にありません: {missing}")
    cycle = dependency_cycle(proposals)
    if cycle is not None:
        raise ValueError(f"Proposal dependency cycle: {' -> '.join(cycle)}")


def quota_admissible(
    proposal: ProposalManifest,
    *,
    remaining: Mapping[str, float],
    reserve: Mapping[str, float],
    multiplier: float = 1.3,
) -> AdmissionResult:
    if multiplier <= 0:
        raise ValueError("multiplier は正数でなければなりません")
    for estimate in proposal.budget_estimate.pool_quota:
        if estimate.pool_id not in remaining or estimate.pool_id not in reserve:
            return AdmissionResult(False, f"pool {estimate.pool_id} の remaining/reserve が不明です")
        if multiplier * estimate.amount + reserve[estimate.pool_id] > remaining[estimate.pool_id]:
            return AdmissionResult(False, f"pool {estimate.pool_id} の quota が不足しています")
    return AdmissionResult(True)


def admit(
    proposal: ProposalManifest,
    *,
    active_proposal: ProposalManifest | None,
    done_ids: Iterable[str],
    remaining: Mapping[str, float],
    reserve: Mapping[str, float],
) -> AdmissionResult:
    if active_proposal is not None:
        return AdmissionResult(False, "active Proposal slot は既に使用中です")
    if not dependencies_satisfied(proposal, done_ids):
        return AdmissionResult(False, "依存 Proposal が DONE ではありません")
    quota = quota_admissible(proposal, remaining=remaining, reserve=reserve)
    if not quota.admissible:
        return quota
    return AdmissionResult(True)


def choose(
    candidates: Iterable[ProposalManifest],
    *,
    active_proposal: ProposalManifest | None,
    done_ids: Iterable[str],
    remaining: Mapping[str, float],
    reserve: Mapping[str, float],
) -> ProposalManifest | None:
    admissible = [
        candidate
        for candidate in candidates
        if admit(
            candidate,
            active_proposal=active_proposal,
            done_ids=done_ids,
            remaining=remaining,
            reserve=reserve,
        ).admissible
    ]
    if not admissible:
        return None
    return sorted(admissible, key=lambda item: (item.created_at, item.id))[0]


__all__ = [
    "AdmissionResult",
    "admit",
    "choose",
    "dependencies_satisfied",
    "dependency_cycle",
    "quota_admissible",
    "scope_overlaps",
    "validate_dependency_graph",
]
