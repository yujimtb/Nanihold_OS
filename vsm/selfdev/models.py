"""自己開発ループの永続化モデル。

このモジュールのモデルは controller の入力と永続 artifact の境界で使う。
任意の shell、未知の enum、scope 外の verifier path は受け付けない。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vsm.clock import format_iso_ms
from vsm.ids import generate_run_id

_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
_PROPOSAL_ID = r"^proposal-[0-9a-f]{32}$"
_SHA256 = r"^[0-9a-f]{64}$"
_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_assignment=True)


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("path は空文字・NUL を含められません")
    value = value.replace("\\", "/")
    if value.startswith("/") or PurePosixPath(value).is_absolute():
        raise ValueError("path は repository-relative でなければなりません")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path は .、..、空の要素を含められません")
    return value.rstrip("/")


def _timestamp(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(_TS, value):
        raise ValueError("UTC ISO 8601 millisecond timestamp が必要です")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp は UTC でなければなりません")
    return value


class PathRule(StrictModel):
    path: str
    kind: Literal["file", "tree"]

    _normalise = field_validator("path")(_path)


def path_in_scope(path: str, scope: tuple[PathRule, ...] | list[PathRule]) -> bool:
    value = _path(path)
    return any(
        value == rule.path
        or (rule.kind == "tree" and value.startswith(f"{rule.path}/"))
        for rule in scope
    )


def is_protected_path(path: str) -> bool:
    """trusted control plane に固定された protected classifier。"""

    value = _path(path)
    if value in {"AGENTS.md", "vsm.toml", "openspec/project.md"}:
        return True
    if value == ".github" or value.startswith(".github/"):
        return True
    if value == "vsm/gates" or value.startswith("vsm/gates/"):
        return True
    if value.startswith("openspec/changes/"):
        filename = value.rsplit("/", 1)[-1]
        if filename in {"proposal.md", "design.md", "spec.md"}:
            return True
        if "/specs/" in value or value.endswith("/specs"):
            return True
    return False


class GateStatusVerifier(StrictModel):
    kind: Literal["gate_status"]
    gate: Literal["g1", "g2", "g3", "g4"]


class PathVerifier(StrictModel):
    kind: Literal["path_exists", "path_absent"]
    path: str
    _normalise = field_validator("path")(_path)


class FileLiteralVerifier(StrictModel):
    kind: Literal["file_contains", "file_not_contains"]
    path: str
    literal: str = Field(min_length=1)
    _normalise = field_validator("path")(_path)


class JsonPointerVerifier(StrictModel):
    kind: Literal["json_pointer_equals"]
    path: str
    pointer: str = Field(min_length=1)
    expected: Any
    _normalise = field_validator("path")(_path)


Verifier: TypeAlias = (
    GateStatusVerifier | PathVerifier | FileLiteralVerifier | JsonPointerVerifier
)


class AcceptanceCriterion(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    statement: str = Field(min_length=1)
    verifier: Verifier = Field(discriminator="kind")


class QuotaEstimate(StrictModel):
    pool_id: str = Field(min_length=1, max_length=64)
    unit: Literal["usage_percent", "tokens", "requests"]
    amount: float = Field(gt=0)


class BudgetEstimate(StrictModel):
    tokens: int = Field(ge=0)
    active_wall_clock_seconds: int = Field(ge=0)
    pool_quota: tuple[QuotaEstimate, ...] = ()

    @field_validator("pool_quota")
    @classmethod
    def _unique_pools(cls, value: tuple[QuotaEstimate, ...]) -> tuple[QuotaEstimate, ...]:
        ids = [item.pool_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("pool_quota の pool_id は unique でなければなりません")
        return value


class ActorRef(StrictModel):
    actor_type: Literal["human", "fable", "scheduler", "s4"]
    actor_id: str = Field(min_length=1, max_length=128)


class _OriginBase(StrictModel):
    decision_ref: str = Field(min_length=1)
    roadmap_ref: str | None = None
    openspec_ref: str | None = None
    conversation_id: str | None = None
    finding_event_id: str | None = None


class ConversationOrigin(_OriginBase):
    kind: Literal["conversation"]

    @model_validator(mode="after")
    def _requires_conversation(self) -> "ConversationOrigin":
        if not self.conversation_id:
            raise ValueError("conversation origin には conversation_id が必要です")
        return self


class ReadyQueueOrigin(_OriginBase):
    kind: Literal["ready_queue"]

    @model_validator(mode="after")
    def _requires_source(self) -> "ReadyQueueOrigin":
        if not self.roadmap_ref and not self.openspec_ref:
            raise ValueError("ready_queue origin には roadmap_ref または openspec_ref が必要です")
        return self


class S4FindingOrigin(_OriginBase):
    kind: Literal["s4_finding"]

    @model_validator(mode="after")
    def _requires_finding(self) -> "S4FindingOrigin":
        if not self.finding_event_id:
            raise ValueError("s4_finding origin には finding_event_id が必要です")
        return self


ProposalOrigin: TypeAlias = ConversationOrigin | ReadyQueueOrigin | S4FindingOrigin


class ProposalManifest(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(pattern=_PROPOSAL_ID)
    title: str = Field(min_length=1, max_length=160)
    motivation: str = Field(min_length=1)
    scope: tuple[PathRule, ...] = Field(min_length=1)
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1)
    risk_class: Literal["low", "normal", "protected"]
    budget_estimate: BudgetEstimate
    origin: ProposalOrigin = Field(discriminator="kind")
    dependencies: tuple[str, ...] = ()
    created_at: str
    created_by: ActorRef

    _validate_timestamp = field_validator("created_at")(_timestamp)

    @field_validator("dependencies")
    @classmethod
    def _dependencies_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("dependencies は unique でなければなりません")
        return value

    @model_validator(mode="after")
    def _semantic_validation(self) -> "ProposalManifest":
        if self.id in self.dependencies:
            raise ValueError("Proposal は自分自身に依存できません")
        protected = any(is_protected_path(rule.path) for rule in self.scope)
        if protected and self.risk_class != "protected":
            raise ValueError("protected path を含む Proposal は risk_class=protected が必要です")
        for criterion in self.acceptance_criteria:
            verifier = criterion.verifier
            path = getattr(verifier, "path", None)
            if path is not None and not path_in_scope(path, self.scope):
                raise ValueError(
                    f"acceptance criterion {criterion.id} の path が Proposal scope 外です"
                )
        return self

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)

    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class RunRuntime(StrictModel):
    role: Literal["S1_WORKER", "S4_SCANNER"]
    backend: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)


class GateResult(StrictModel):
    status: Literal["pass", "fail", "skip", "error"]
    duration_ms: int = Field(ge=0)
    summary: str = ""
    highlights: tuple[str, ...] = ()
    log_ref: str = Field(min_length=1)
    log_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _log_ref_is_relative(self) -> "GateResult":
        if self.log_ref.startswith("/") or "\\" in self.log_ref or "\x00" in self.log_ref:
            raise ValueError("gate log_ref は repository-relative の slash path でなければなりません")
        if any(part in {"", ".", ".."} for part in self.log_ref.split("/")):
            raise ValueError("gate log_ref に不正な path 要素があります")
        return self


class GateReport(StrictModel):
    schema_version: Literal[2] = 2
    proposal_id: str = Field(pattern=_PROPOSAL_ID)
    implementation_run_id: str = Field(min_length=1)
    gate_attempt: Literal[1, 2]
    generated_at: str
    worktree_path: str = Field(min_length=1)
    report_ref: str | None = None
    base_sha: str = Field(min_length=1)
    scope_sha256: str = Field(pattern=_SHA256)
    candidate_diff_sha256: str = Field(pattern=_SHA256)
    gates_requested: tuple[Literal["g1", "g2", "g3", "g4"], ...]
    status: Literal["pass", "fail", "error"]
    exit_code: int
    changed_paths: tuple[str, ...] = ()
    scope_violations: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    protected_approval_event_id: str | None = None
    gates: Mapping[str, GateResult]

    _validate_timestamp = field_validator("generated_at")(_timestamp)

    @field_validator("gates_requested")
    @classmethod
    def _required_gates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != ("g1", "g2", "g3", "g4"):
            raise ValueError("gates_requested は g1,g2,g3,g4 固定です")
        return value

    @model_validator(mode="after")
    def _gate_map_is_complete(self) -> "GateReport":
        if set(self.gates) != {"g1", "g2", "g3", "g4"}:
            raise ValueError("gates は g1,g2,g3,g4 を一度ずつ含まなければなりません")
        return self


class Auditor(StrictModel):
    node_id: str = Field(min_length=1)
    role: Literal["S3STAR_AUDITOR"]
    backend: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    # 監査は毎回 session_ref なしで開始し、runtime が返す参照も永続化しない。
    session_ref: str | None = None
    independent: Literal[True]


class AuditCandidate(StrictModel):
    base_sha: str = Field(min_length=1)
    commit_sha: str = Field(min_length=1)
    tree_sha: str = Field(min_length=1)
    diff_ref: str = Field(min_length=1)
    diff_sha256: str = Field(pattern=_SHA256)


class AuditInputs(StrictModel):
    proposal_manifest_ref: str = Field(min_length=1)
    proposal_manifest_sha256: str = Field(pattern=_SHA256)
    gate_report_ref: str = Field(min_length=1)
    gate_report_sha256: str = Field(pattern=_SHA256)
    raw_logs: tuple[str, ...] = ()


class AcceptanceResult(StrictModel):
    criterion_id: str = Field(min_length=1)
    status: Literal["pass", "fail", "indeterminate"]
    evidence_refs: tuple[str, ...] = ()
    finding: str = ""


class ScopeCheck(StrictModel):
    status: Literal["pass", "fail", "indeterminate"]
    changed_paths: tuple[str, ...] = ()
    outside_scope_paths: tuple[str, ...] = ()


class AuditBudget(StrictModel):
    estimate: Mapping[str, Any]
    actual: Mapping[str, Any]
    variance: Mapping[str, Any]


class AuditFinding(StrictModel):
    finding_id: str = Field(min_length=1)
    severity: Literal["info", "warning", "error", "critical"]
    category: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()


class AuditReport(StrictModel):
    schema_version: Literal[1] = 1
    audit_id: str = Field(min_length=1)
    proposal_id: str = Field(pattern=_PROPOSAL_ID)
    generated_at: str
    auditor: Auditor
    candidate: AuditCandidate
    inputs: AuditInputs
    acceptance_results: tuple[AcceptanceResult, ...] = Field(min_length=1)
    scope_check: ScopeCheck
    budget: AuditBudget
    findings: tuple[AuditFinding, ...] = ()
    verdict: Literal["pass", "fail", "indeterminate"]
    summary: str = Field(min_length=1)

    _validate_timestamp = field_validator("generated_at")(_timestamp)


class ConsortiumDecision(StrictModel):
    consortium_id: str = Field(min_length=1)
    proposal_id: str = Field(pattern=_PROPOSAL_ID)
    review_kind: Literal["initial", "final"]
    decision: Literal["APPROVE", "REJECT", "MERGE_READY", "REJECT_FINAL"]
    reason: str = Field(min_length=1)
    dissent_summary: str = ""
    conditions: tuple[str, ...] = ()
    residual_risks: tuple[str, ...] = ()
    merge_recommendation_reason: str | None = None
    dossier_ref: str = Field(min_length=1)
    dossier_sha256: str = Field(pattern=_SHA256)
    human_participated: bool
    human_timed_out: bool

    @model_validator(mode="after")
    def _final_fields(self) -> "ConsortiumDecision":
        if self.review_kind == "final" and not self.merge_recommendation_reason:
            raise ValueError("final Consortium decision には merge_recommendation_reason が必要です")
        return self


class PRDescription(StrictModel):
    """typed data から決定論的にレンダリングする PR 説明。"""

    proposal: ProposalManifest
    initial_decision: ConsortiumDecision
    protected_approval: str | None
    candidate_commit: str = Field(min_length=1)
    diff_summary: str = Field(min_length=1)
    gate_report: GateReport
    audit_report: AuditReport
    budget_actual: Mapping[str, Any]
    final_decision: ConsortiumDecision
    artifact_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _decisions_match(self) -> "PRDescription":
        if self.initial_decision.review_kind != "initial":
            raise ValueError("initial_decision は initial review でなければなりません")
        if self.final_decision.review_kind != "final":
            raise ValueError("final_decision は final review でなければなりません")
        if self.initial_decision.decision not in {"APPROVE", "REJECT"}:
            raise ValueError("initial_decision の decision が不正です")
        if self.final_decision.decision not in {"MERGE_READY", "REJECT_FINAL"}:
            raise ValueError("final_decision の decision が不正です")
        if self.proposal.risk_class == "protected" and not self.protected_approval:
            raise ValueError("protected Proposal には Human approval が必要です")
        return self

    def render(self) -> str:
        p = self.proposal
        initial = self.initial_decision
        final = self.final_decision
        scope = "\n".join(f"- `{r.path}` ({r.kind})" for r in p.scope)
        criteria = "\n".join(f"- {c.id}: {c.statement}" for c in p.acceptance_criteria)
        gates = "\n".join(
            f"- {name}: {result.status} — {result.summary or '（記録なし）'}"
            for name, result in sorted(self.gate_report.gates.items())
        )
        findings = "\n".join(
            f"- {finding.severity}: {finding.summary}" for finding in self.audit_report.findings
        ) or "- なし"
        artifacts = "\n".join(f"- `{ref}`" for ref in self.artifact_refs) or "- なし"
        return (
            f"# {p.title}\n\n"
            f"## Proposal\n`{p.id}` / risk=`{p.risk_class}`\n\n"
            f"## 動機\n{p.motivation}\n\n"
            f"## 変更scope\n{scope}\n\n"
            f"## 受入条件\n{criteria}\n\n"
            f"## 初回Consortium決定\n{initial.decision}: {initial.reason}\n"
            f"反対意見: {initial.dissent_summary or 'なし'}\n\n"
            f"## Human protected approval\n{self.protected_approval or 'なし'}\n\n"
            f"## 変更概要\n{self.diff_summary}\n\n"
            f"## Gate結果\n{gates}\n\n"
            f"## S3★独立監査\nverdict: {self.audit_report.verdict}\n"
            f"{self.audit_report.summary}\n{findings}\n\n"
            f"## 予算見積と実績\n{json.dumps(self.budget_actual, ensure_ascii=False, sort_keys=True)}\n\n"
            f"## 最終Consortium決定\n{final.decision}: {final.reason}\n\n"
            f"### マージ推奨理由\n{final.merge_recommendation_reason or 'なし'}\n\n"
            f"### 残リスク\n{chr(10).join(f'- {risk}' for risk in final.residual_risks) or '- なし'}\n\n"
            f"### 反対意見の要約\n{final.dissent_summary or 'なし'}\n\n"
            f"## 成果物\n{artifacts}\n\n"
            "## Human向け手順\ncandidate branch を確認し、必要なら人間が push/merge してください。\n"
        )


def proposal_to_run_manifest(
    proposal: ProposalManifest,
    *,
    repository: Any,
    base_sha: str,
    worktree_path: Any,
    initial_decision_event_id: str,
    writer_runtime: RunRuntime,
    run_id: str | None = None,
    attempt: Literal[1, 2] = 1,
    parent_run_id: str | None = None,
    analysis_runtime: RunRuntime | None = None,
    protected_approval_event_id: str | None = None,
    created_at: str | None = None,
) -> Any:
    """Proposal の immutable 値を RunManifest の新契約へ deep copy する。"""

    from vsm.runtime.manifest import RunManifest

    if attempt == 2 and not parent_run_id:
        raise ValueError("repair Run には parent_run_id が必要です")
    if attempt == 1 and parent_run_id is not None:
        raise ValueError("implementation Run に parent_run_id は指定できません")
    if proposal.risk_class == "protected" and not protected_approval_event_id:
        raise ValueError("protected Proposal には Human approval event が必要です")
    return RunManifest(
        schema_version=1,
        run_id=run_id or generate_run_id(),
        proposal_id=proposal.id,
        attempt=attempt,
        run_kind="implementation" if attempt == 1 else "repair",
        parent_run_id=parent_run_id,
        repository=repository,
        base_sha=base_sha,
        branch=f"selfdev/{proposal.id}",
        worktree_path=worktree_path,
        proposal_manifest_ref=f"runs/selfdev/proposals/{proposal.id}/proposal.json",
        proposal_manifest_sha256=proposal.sha256(),
        scope=tuple(rule.model_dump(mode="json") for rule in proposal.scope),
        scope_sha256=hashlib.sha256(
            json.dumps(
                [rule.model_dump(mode="json") for rule in proposal.scope],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        acceptance_criteria=tuple(
            criterion.model_dump(mode="json") for criterion in proposal.acceptance_criteria
        ),
        required_gates=("g1", "g2", "g3", "g4"),
        writer_runtime=writer_runtime.model_dump(mode="json"),
        analysis_runtime=(analysis_runtime.model_dump(mode="json") if analysis_runtime else None),
        budget=proposal.budget_estimate.model_dump(mode="json"),
        risk_class=proposal.risk_class,
        initial_decision_event_id=initial_decision_event_id,
        protected_approval_event_id=protected_approval_event_id,
        created_at=created_at or format_iso_ms(datetime.now(timezone.utc)),
    )
