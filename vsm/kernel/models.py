from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9._~-]{1,160}$")]
BlobRef = Annotated[str, Field(pattern=r"^blob:sha256:[0-9a-f]{64}$")]
NonBlank = Annotated[str, Field(min_length=1, max_length=512)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataSpaceKind(StrEnum):
    PERSONAL = "personal"
    COMPANY = "company"
    SANDBOX = "sandbox"


class DataSpace(StrictModel):
    data_space_id: Identifier
    owner_id: Identifier
    kind: DataSpaceKind
    lethe_location: NonBlank


class VSMFunction(StrEnum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S3_STAR = "S3*"
    S4 = "S4"
    S5 = "S5"


class NodeKind(StrEnum):
    ORGANIZATION = "organization"
    UNIT = "unit"
    INTERFACE = "interface"
    EXPERIMENT = "experiment"


class NodeStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class UVSMNode(StrictModel):
    node_id: Identifier
    data_space_id: Identifier
    owner_id: Identifier
    name: NonBlank
    kind: NodeKind
    parent_node_id: Identifier | None
    resident_functions: frozenset[VSMFunction]
    resident_s3_parent_function: Literal[VSMFunction.S5]
    status: NodeStatus
    memory_stream_id: Identifier

    @model_validator(mode="after")
    def viable_and_interface_owned(self) -> "UVSMNode":
        required = frozenset(VSMFunction)
        if self.resident_functions != required:
            raise ValueError("every UVSMNode must contain resident S1-S5 and S3* functions")
        if self.kind is NodeKind.INTERFACE and self.parent_node_id is not None:
            raise ValueError(
                "an owner Interface Node must not be a child of a company UVSMNode"
            )
        return self


class WorkState(StrEnum):
    PROPOSED = "proposed"
    READY = "ready"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkEdgeKind(StrEnum):
    DELEGATED_TO = "delegated_to"
    DEPENDS_ON = "depends_on"
    INTEGRATED_BY = "integrated_by"


class WorkEdge(StrictModel):
    source_work_item_id: Identifier
    target_work_item_id: Identifier
    kind: WorkEdgeKind


class CompletionEvidence(StrictModel):
    acceptance_satisfied: bool
    required_tests_passed: bool
    blocking_deviations: tuple[NonBlank, ...]
    independent_s3_star_gate: bool
    integration_branch_merged: bool
    remote_push_succeeded: bool


class WorkItem(StrictModel):
    work_item_id: Identifier
    data_space_id: Identifier
    title: NonBlank
    description: NonBlank
    owner_node_id: Identifier
    delegated_to_node_id: Identifier
    integration_owner_node_id: Identifier
    parent_work_item_id: Identifier | None
    acceptance_criteria: tuple[NonBlank, ...]
    route_key: NonBlank
    state: WorkState
    blocking_s3_star_finding_ids: tuple[Identifier, ...]
    completion_evidence: CompletionEvidence | None


class ExecutionState(StrEnum):
    REQUESTED = "requested"
    ACTIVE = "active"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Execution(StrictModel):
    execution_id: Identifier
    data_space_id: Identifier
    node_id: Identifier
    work_item_id: Identifier
    pilot_id: Identifier
    model_candidate_key: NonBlank
    state: ExecutionState
    provider_session_id: NonBlank | None
    pilot_host_id: Identifier
    pause_reason: NonBlank | None


class GrantState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class CapabilityGrant(StrictModel):
    grant_id: Identifier
    data_space_id: Identifier
    grantor_node_id: Identifier
    grantee_node_id: Identifier
    capabilities: frozenset[NonBlank]
    purpose: NonBlank
    valid_from: datetime
    expires_at: datetime
    state: GrantState

    @model_validator(mode="after")
    def interval_is_positive(self) -> "CapabilityGrant":
        if self.expires_at <= self.valid_from:
            raise ValueError("CapabilityGrant expires_at must follow valid_from")
        return self


class EffectLeaseState(StrEnum):
    PLANNED = "planned"
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    REVOKED = "revoked"


class EffectLease(StrictModel):
    lease_id: Identifier
    data_space_id: Identifier
    work_item_id: Identifier
    execution_id: Identifier
    effect_kind: NonBlank
    target: NonBlank
    idempotency_key: NonBlank
    plan_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    state: EffectLeaseState
    expires_at: datetime


class EffectApproval(StrictModel):
    lease_id: Identifier
    approved_by: Identifier
    approved_at: datetime


class ReferenceGrant(StrictModel):
    grant_id: Identifier
    source_data_space_id: Identifier
    target_data_space_id: Identifier
    granted_by: Identifier
    purpose: NonBlank
    subject_refs: tuple[Identifier, ...]
    valid_from: datetime
    expires_at: datetime
    state: GrantState

    @model_validator(mode="after")
    def isolates_spaces(self) -> "ReferenceGrant":
        if self.source_data_space_id == self.target_data_space_id:
            raise ValueError("ReferenceGrant must cross two different DataSpaces")
        if not self.subject_refs:
            raise ValueError("ReferenceGrant subject_refs must not be empty")
        if self.expires_at <= self.valid_from:
            raise ValueError("ReferenceGrant expires_at must follow valid_from")
        return self


class BudgetReservationState(StrEnum):
    RESERVED = "reserved"
    CONSUMED = "consumed"
    RELEASED = "released"


class BudgetReservation(StrictModel):
    reservation_id: Identifier
    data_space_id: Identifier
    work_item_id: Identifier
    currency: NonBlank
    amount: Annotated[float, Field(gt=0)]
    token_limit: Annotated[int, Field(gt=0)]
    state: BudgetReservationState


class AuditPolicy(StrictModel):
    policy_id: Identifier
    data_space_id: Identifier
    independent_s3_star_required: Literal[True]
    raw_drill_down_required: Literal[True]
    retention_days: Annotated[int, Field(gt=0)]


class ControlPolicy(StrictModel):
    policy_id: Identifier
    data_space_id: Identifier
    stop_scope: Literal["affected_work_and_effects"]
    severe_finding_requires_s5_risk_acceptance: Literal[True]
    completion_requires_remote_push: Literal[True]


class EventEnvelope(StrictModel):
    event_id: Identifier
    data_space_id: Identifier
    stream_id: Identifier
    stream_version: Annotated[int, Field(gt=0)]
    event_type: NonBlank
    occurred_at: datetime
    actor_type: Literal["human", "kernel", "pilot", "system"]
    actor_id: Identifier | None
    correlation_id: Identifier | None
    causation_id: Identifier | None
    idempotency_key: NonBlank
    payload: dict[str, Any]


class AppendResult(StrictModel):
    outcome: Literal["appended", "duplicate"]
    cursor: Annotated[int, Field(gt=0)]
    stream_version: Annotated[int, Field(gt=0)]


class StoredEvent(StrictModel):
    cursor: Annotated[int, Field(gt=0)]
    event: EventEnvelope


class S3StarSeverity(StrEnum):
    ADVISORY = "advisory"
    SEVERE = "severe"


class S3StarFinding(StrictModel):
    finding_id: Identifier
    data_space_id: Identifier
    work_item_id: Identifier
    node_id: Identifier
    severity: S3StarSeverity
    statement: NonBlank
    evidence_refs: tuple[Identifier, ...]
    accepted_by_s5: bool


class RouteSnapshotState(StrEnum):
    DRAFT = "draft"
    S3_STAR_APPROVED = "s3_star_approved"
    OWNER_APPROVED = "owner_approved"
    PUBLISHED = "published"


class RouteSnapshot(StrictModel):
    snapshot_id: Identifier
    data_space_id: Identifier
    route_key: NonBlank
    evidence_cursor: Annotated[int, Field(ge=0)]
    candidate_keys: tuple[NonBlank, ...]
    production_objective: Literal[
        "reliability_then_cost", "expected_utility", "quality_max"
    ]
    state: RouteSnapshotState
    s3_star_approval_event_id: Identifier | None
    owner_approval_event_id: Identifier | None

    @model_validator(mode="after")
    def candidates_and_approvals_are_consistent(self) -> "RouteSnapshot":
        if not self.candidate_keys or len(self.candidate_keys) != len(
            set(self.candidate_keys)
        ):
            raise ValueError(
                "RouteSnapshot candidate_keys must be non-empty and unique"
            )
        if self.state is RouteSnapshotState.DRAFT:
            if (
                self.s3_star_approval_event_id is not None
                or self.owner_approval_event_id is not None
            ):
                raise ValueError("draft RouteSnapshot cannot contain approvals")
        elif self.state is RouteSnapshotState.S3_STAR_APPROVED:
            if (
                self.s3_star_approval_event_id is None
                or self.owner_approval_event_id is not None
            ):
                raise ValueError("S3*-approved RouteSnapshot approval fields mismatch")
        elif (
            self.s3_star_approval_event_id is None
            or self.owner_approval_event_id is None
        ):
            raise ValueError("owner-approved RouteSnapshot requires both approvals")
        return self
