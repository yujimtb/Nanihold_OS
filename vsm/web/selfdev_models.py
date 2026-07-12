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
    action: Literal["suspend", "resume", "abort"]
    reason: str = Field(min_length=1)
    expected_state_version: int = Field(ge=1)


class HumanDecisionBody(SelfDevRequestModel):
    decision: Literal["approve", "reject", "respond"]
    reason: str = ""
    statement: str | None = None
    expected_state_version: int = Field(ge=1)
    proposal_manifest_sha256: str | None = None
    protected_scope_sha256: str | None = None

    @model_validator(mode="after")
    def validate_text(self) -> "HumanDecisionBody":
        if self.decision == "respond":
            if not self.statement or not self.statement.strip():
                raise ValueError("respond には statement が必要です")
        elif not self.reason.strip():
            raise ValueError("approve/reject には reason が必要です")
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
