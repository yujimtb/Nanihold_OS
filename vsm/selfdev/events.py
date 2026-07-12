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
        if self.cause == "QUOTA_WAIT" and (not self.pool_id or not self.reset_at):
            raise ValueError("QUOTA_WAIT pause event には pool_id/reset_at が必要です")
        if self.cause == "SUSPEND" and (self.pool_id is not None or self.reset_at is not None):
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


SELFDEV_EVENT_TYPES: tuple[str, ...] = (
    "proposal_state_changed",
    "proposal_pause_changed",
    "proposal_run_linked",
)

SELFDEV_PAYLOAD_MODELS: dict[tuple[str, int], type[BaseModel]] = {
    ("proposal_state_changed", 1): ProposalStateChangedPayload,
    ("proposal_pause_changed", 1): ProposalPauseChangedPayload,
    ("proposal_run_linked", 1): ProposalRunLinkedPayload,
    ("artifact_created", 2): ArtifactCreatedPayload,
    ("audit_report_sent", 2): AuditReportSentV1Payload,
    ("consortium_decided", 2): ConsortiumDecidedV2Payload,
    ("gate_report_generated", 2): GateReportGeneratedV2Payload,
}

__all__ = [
    "ArtifactCreatedPayload",
    "AuditReportSentV1Payload",
    "ConsortiumDecidedV2Payload",
    "GateReportGeneratedV2Payload",
    "ProposalPauseChangedPayload",
    "ProposalRunLinkedPayload",
    "ProposalStateChangedPayload",
    "SELFDEV_EVENT_TYPES",
    "SELFDEV_PAYLOAD_MODELS",
]
