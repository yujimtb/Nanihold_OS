"""EEP-10 owner approval boundary models."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from pydantic import Field, field_validator, model_validator

from .contracts import EnvironmentContract, EnvironmentModel


class ProcurementPolicyBoundary(EnvironmentModel):
    """Resources, network destinations, and budget available to S3."""

    allowed_resources: tuple[str, ...] = Field(min_length=1)
    allowed_networks: tuple[str, ...] = Field(min_length=1)
    budget_currency: str = Field(pattern=r"^[A-Z]{3}$")
    maximum_budget: Decimal = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def normalize_collections(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        normalized = dict(value)
        for field_name in ("allowed_resources", "allowed_networks"):
            items = normalized.get(field_name)
            if isinstance(items, (list, tuple, set, frozenset)):
                normalized[field_name] = tuple(sorted(str(item).strip() for item in items))
        return normalized

    @field_validator("allowed_resources", "allowed_networks")
    @classmethod
    def values_are_unique_and_nonblank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("ProcurementPolicyBoundary values must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("ProcurementPolicyBoundary values must be unique")
        return value


class ApprovalTargetKind(StrEnum):
    """The only two subjects that can cross the owner approval boundary."""

    ENVIRONMENT_CONTRACT = "environment_contract"
    PROCUREMENT_POLICY_BOUNDARY = "procurement_policy_boundary"


class OwnerApprovalTarget(EnvironmentModel):
    """Complete owner-approved scope; no individual instance operation exists."""

    environment_contract: EnvironmentContract
    procurement_policy_boundary: ProcurementPolicyBoundary

    @property
    def target_kinds(self) -> tuple[ApprovalTargetKind, ApprovalTargetKind]:
        return (
            ApprovalTargetKind.ENVIRONMENT_CONTRACT,
            ApprovalTargetKind.PROCUREMENT_POLICY_BOUNDARY,
        )


class OwnerApprovalRequest(EnvironmentModel):
    """Approval request limited to contract and procurement policy boundary."""

    target: OwnerApprovalTarget
