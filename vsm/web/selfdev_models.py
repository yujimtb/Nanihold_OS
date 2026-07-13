"""自己開発 REST API の strict transport model。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vsm.selfdev.models import (
    AcceptanceCriterion,
    BudgetEstimate,
    PathRule,
    ProposalOrigin,
)


class SelfDevRequestModel(BaseModel):
    """API では未知フィールドを受け付けない。"""

    model_config = ConfigDict(extra="forbid")


class ProposalCreateBody(SelfDevRequestModel):
    title: str = Field(min_length=1, max_length=160)
    motivation: str = Field(min_length=1)
    scope: tuple[PathRule, ...] = Field(min_length=1)
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1)
    risk_class: Literal["low", "normal", "protected"]
    budget_estimate: BudgetEstimate
    origin: ProposalOrigin = Field(discriminator="kind")
    dependencies: tuple[str, ...] = ()


class ProposalControlBody(SelfDevRequestModel):
    action: Literal["suspend", "resume", "abort", "force_abort"]
    reason: str = Field(min_length=1)
    expected_state_version: int = Field(ge=1)
    pause_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_pause_id(self) -> "ProposalControlBody":
        if self.action != "resume" and self.pause_id is not None:
            raise ValueError("pause_id は resume でのみ指定できます")
        return self


class HumanDecisionBody(SelfDevRequestModel):
    decision: Literal["approve", "reject", "respond", "completed", "failed"]
    reason: str = ""
    statement: str | None = None
    expected_state_version: int = Field(ge=1)
    proposal_manifest_sha256: str | None = None
    protected_scope_sha256: str | None = None
    effect_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_text(self) -> "HumanDecisionBody":
        effect_decision = self.decision in {"completed", "failed"}
        if effect_decision and self.effect_id is None:
            raise ValueError("in-doubt 効果の裁定には effect_id が必要です")
        if not effect_decision and self.effect_id is not None:
            raise ValueError("effect_id は in-doubt 効果の裁定でのみ指定できます")
        if self.decision == "respond":
            if not self.statement or not self.statement.strip():
                raise ValueError("respond には statement が必要です")
        elif not self.reason.strip():
            raise ValueError("approve/reject/completed/failed には reason が必要です")
        return self


class MergeOutcomeBody(SelfDevRequestModel):
    merged: bool
    reason: str = ""
    merge_sha: str | None = None

    @model_validator(mode="after")
    def validate_archive_reason(self) -> "MergeOutcomeBody":
        if not self.merged and not self.reason.strip():
            raise ValueError("archived outcome には reason が必要です")
        return self


__all__ = [
    "HumanDecisionBody",
    "MergeOutcomeBody",
    "ProposalControlBody",
    "ProposalCreateBody",
]
