"""自己開発 Event stream の version 1 payload schema。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vsm.selfdev.state_machine import ProposalPhase


class SelfDevPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _relative_ref(value: str) -> str:
    if not value or value.startswith("/") or "\x00" in value:
        raise ValueError("artifact ref は Proposal root 相対 path でなければなりません")
    parts = value.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("artifact ref に不正な path 要素があります")
    return value.replace("\\", "/")


class ProposalStateChangedPayload(SelfDevPayload):
    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    from_state: ProposalPhase | None
    to_state: ProposalPhase
    reason_code: Literal[
        "proposal_created", "review_started", "consortium_approved", "consortium_rejected",
        "human_decision_required", "human_approved", "human_rejected", "human_timeout",
        "workspace_ready", "implementation_started", "implementation_completed", "gates_passed",
        "gates_failed", "repair_completed", "repair_exhausted", "audit_started", "audit_completed",
        "audit_failed", "final_approved", "final_rejected", "merged", "archived", "aborted",
    ]
    reason: str = Field(min_length=1)
    related_run_id: str | None = None
    decision_event_id: str | None = None
    artifact_refs: tuple[str, ...] = ()

    @field_validator("artifact_refs")
    @classmethod
    def _refs(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_ref(ref) for ref in refs)


class ProposalPauseChangedPayload(SelfDevPayload):
    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    action: Literal["added", "removed"]
    pause_id: str = Field(min_length=1)
    cause: Literal["SUSPEND", "QUOTA_WAIT"]
    actor_type: Literal["human", "node", "controller"] | None = None
    actor_id: str | None = None
    pool_id: str | None = None
    reset_at: str | None = None
    source_event_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)

    def model_post_init(self, __context: Any) -> None:
        if self.action == "added" and self.cause == "QUOTA_WAIT" and (not self.pool_id or not self.reset_at):
            raise ValueError("QUOTA_WAIT pause event には pool_id/reset_at が必要です")
        if self.action == "added" and self.cause == "SUSPEND" and (self.pool_id is not None or self.reset_at is not None):
            raise ValueError("SUSPEND pause event に pool_id/reset_at は指定できません")
        if self.action == "removed" and self.source_event_id is None:
            raise ValueError("pause remove event には source_event_id が必要です")


class ProposalRunLinkedPayload(SelfDevPayload):
    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    run_id: str = Field(min_length=1)
    run_kind: Literal["initial_review", "implementation", "repair", "audit", "final_review"]
    attempt: Literal[1, 2]
    parent_run_id: str | None = None
    manifest_ref: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def model_post_init(self, __context: Any) -> None:
        if self.run_kind == "repair":
            if self.attempt != 2 or not self.parent_run_id:
                raise ValueError("repair Run は attempt=2 と parent_run_id が必要です")
        elif self.attempt != 1 or self.parent_run_id is not None:
            raise ValueError("repair 以外の Run は attempt=1 かつ parent_run_id=null が必要です")


class ProposalIntegrityFailedPayload(SelfDevPayload):
    """Proposal 単位の immutable integrity failure を durable に記録する。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    phase: ProposalPhase
    disposition: Literal["isolated", "needs_human"]
    failure_kind: Literal[
        "proposal_manifest_missing",
        "proposal_manifest_invalid",
        "proposal_manifest_id_mismatch",
        "proposal_manifest_hash_mismatch",
        "artifact_missing",
        "artifact_hash_mismatch",
    ]
    artifact_ref: str | None = None
    reason: str = Field(min_length=1)

    @field_validator("artifact_ref")
    @classmethod
    def _artifact_ref(cls, value: str | None) -> str | None:
        return _relative_ref(value) if value is not None else None


class ProposalIntegrityResolvedPayload(SelfDevPayload):
    """Human/control plane が Proposal 単位の integrity 隔離を解決した記録。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    decision: Literal["approve", "reject", "abort"]
    failure_event_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SelfDevEffectDecidedPayload(SelfDevPayload):
    """Human が in-doubt effect の外部事実を裁定した記録。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    effect_id: str = Field(min_length=1)
    decision: Literal["completed", "failed"]
    reason: str = Field(min_length=1)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    journal_event_id: str = Field(min_length=1)


class SelfDevForceAbortedPayload(SelfDevPayload):
    """cleanup を実行せず artifact を保全して terminal 化した記録。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    reason: str = Field(min_length=1)
    preserved_artifact_refs: tuple[str, ...] = ()

    @field_validator("preserved_artifact_refs")
    @classmethod
    def _refs(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_ref(ref) for ref in refs)


class ConsortiumDecidedV2Payload(SelfDevPayload):
    consortium_id: str = Field(min_length=1)
    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    review_kind: Literal["initial", "final"]
    decision: Literal["APPROVE", "REJECT", "MERGE_READY", "REJECT_FINAL"]
    reason: str = Field(min_length=1)
    dissent_summary: str = ""
    conditions: tuple[str, ...] = ()
    residual_risks: tuple[str, ...] = ()
    merge_recommendation_reason: str | None = None
    dossier_ref: str = Field(min_length=1)
    dossier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    human_participated: bool
    human_timed_out: bool

    def model_post_init(self, __context: Any) -> None:
        if self.review_kind == "final" and not self.merge_recommendation_reason:
            raise ValueError("final consortium_decided には merge_recommendation_reason が必要です")


class ArtifactCreatedPayload(SelfDevPayload):
    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    artifact_kind: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("ref")
    @classmethod
    def _ref(cls, value: str) -> str:
        return _relative_ref(value)


class AuditReportSentV1Payload(SelfDevPayload):
    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    audit_id: str = Field(min_length=1)
    report_ref: str = Field(min_length=1)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GateReportGeneratedV2Payload(SelfDevPayload):
    """Wave 2 の GateReport v2 を参照する strict event payload。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    implementation_run_id: str = Field(min_length=1)
    gate_attempt: Literal[1, 2]
    report_ref: str = Field(min_length=1)
    status: Literal["pass", "fail", "error"]
    gate_statuses: dict[str, Literal["pass", "fail", "skip", "error"]]
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("report_ref")
    @classmethod
    def _report_ref(cls, value: str) -> str:
        return _relative_ref(value)


class ToolInvokedV2Payload(SelfDevPayload):
    """Controller effect journal の開始記録。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    effect_id: str = Field(min_length=1)
    effect_kind: Literal["workspace", "run", "gate", "commit", "cleanup", "audit", "report"]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ToolCompletedV2Payload(SelfDevPayload):
    """Controller effect journal の完了記録。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    effect_id: str = Field(min_length=1)
    effect_kind: Literal["workspace", "run", "gate", "commit", "cleanup", "audit", "report"]
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_refs: tuple[str, ...] = ()
    recovered: bool = False
    recovery_reason: str | None = None

    @field_validator("artifact_refs")
    @classmethod
    def _refs(cls, refs: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_relative_ref(ref) for ref in refs)


class ToolFailedV2Payload(SelfDevPayload):
    """Controller effect journal の失敗記録。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    effect_id: str = Field(min_length=1)
    effect_kind: Literal["workspace", "run", "gate", "commit", "cleanup", "audit", "report"]
    error_type: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    disposition: Literal["operation_failure", "human_decision"] = "operation_failure"


class HumanReviewRequestedV2Payload(SelfDevPayload):
    """再起動しても deadline と review ID を失わない waiter request。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    consortium_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    review_kind: Literal["initial"]
    risk_class: Literal["low", "normal", "protected"]
    deadline: str = Field(min_length=1)
    approval_required: bool


class HumanReviewRespondedV2Payload(SelfDevPayload):
    """Human の statement/approval を Event Log に固定する。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    consortium_id: str = Field(min_length=1)
    review_id: str = Field(min_length=1)
    decision: Literal["statement", "approve", "reject"]
    response: str = Field(min_length=1)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SelfDevWorkspacePathSkippedPayload(SelfDevPayload):
    """workspace scan が管理対象外の Git path を読み飛ばした警告。"""

    proposal_id: str = Field(pattern=r"^proposal-[0-9a-f]{32}$")
    operation: Literal["cleanup", "snapshot", "orphan_detection"]
    path: str = Field(min_length=1)
    reason: str = Field(min_length=1)


SELFDEV_EVENT_TYPES: tuple[str, ...] = (
    "proposal_state_changed",
    "proposal_pause_changed",
    "proposal_run_linked",
    "proposal_integrity_failed",
    "proposal_integrity_resolved",
    "selfdev_workspace_path_skipped",
    "selfdev_effect_decided",
    "selfdev_force_aborted",
)

SELFDEV_PAYLOAD_MODELS: dict[tuple[str, int], type[BaseModel]] = {
    ("proposal_state_changed", 1): ProposalStateChangedPayload,
    ("proposal_pause_changed", 1): ProposalPauseChangedPayload,
    ("proposal_run_linked", 1): ProposalRunLinkedPayload,
    ("proposal_integrity_failed", 1): ProposalIntegrityFailedPayload,
    ("proposal_integrity_resolved", 1): ProposalIntegrityResolvedPayload,
    ("selfdev_effect_decided", 1): SelfDevEffectDecidedPayload,
    ("selfdev_force_aborted", 1): SelfDevForceAbortedPayload,
    ("selfdev_workspace_path_skipped", 1): SelfDevWorkspacePathSkippedPayload,
    ("artifact_created", 2): ArtifactCreatedPayload,
    ("audit_report_sent", 2): AuditReportSentV1Payload,
    ("consortium_decided", 2): ConsortiumDecidedV2Payload,
    ("gate_report_generated", 2): GateReportGeneratedV2Payload,
    ("tool_invoked", 2): ToolInvokedV2Payload,
    ("tool_completed", 2): ToolCompletedV2Payload,
    ("tool_failed", 2): ToolFailedV2Payload,
    ("human_review_requested", 2): HumanReviewRequestedV2Payload,
    ("human_review_responded", 2): HumanReviewRespondedV2Payload,
}

__all__ = [
    "ArtifactCreatedPayload",
    "AuditReportSentV1Payload",
    "ConsortiumDecidedV2Payload",
    "GateReportGeneratedV2Payload",
    "HumanReviewRequestedV2Payload",
    "HumanReviewRespondedV2Payload",
    "ProposalPauseChangedPayload",
    "ProposalRunLinkedPayload",
    "ProposalIntegrityFailedPayload",
    "ProposalIntegrityResolvedPayload",
    "SelfDevEffectDecidedPayload",
    "SelfDevForceAbortedPayload",
    "ProposalStateChangedPayload",
    "SelfDevWorkspacePathSkippedPayload",
    "ToolCompletedV2Payload",
    "ToolFailedV2Payload",
    "ToolInvokedV2Payload",
    "SELFDEV_EVENT_TYPES",
    "SELFDEV_PAYLOAD_MODELS",
]
