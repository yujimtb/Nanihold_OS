from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from vsm.kernel.models import (
    Identifier,
    NonBlank,
    StrictModel,
    UVSMNode,
    WorkEdge,
    WorkItem,
    WorkState,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ActivationState(StrEnum):
    UNCOMMISSIONED = "UNCOMMISSIONED"
    HISTORY_IMPORTED = "HISTORY_IMPORTED"
    REORIENTATION_ONLY = "REORIENTATION_ONLY"
    AWAITING_OWNER_CONFIRMATION = "AWAITING_OWNER_CONFIRMATION"
    ACTIVE = "ACTIVE"


class ReorientationRevisionReason(StrEnum):
    MISSING_RESUME_WORK_ITEM = "missing_resume_work_item"
    OWNER_CORRECTION = "owner_correction"


class ReorientationInterruptionReason(StrEnum):
    PROCESS_RESTART_INTERRUPTED_ATTEMPT = "process_restart_interrupted_attempt"


class HistorySourceKind(StrEnum):
    CLAUDE_CODE = "claude_code"
    CLAUDE_AI = "claude_ai"
    CODEX = "codex"
    INTERCOM = "intercom"
    LETHE = "lethe"
    NANIHOLD_LEGACY = "nanihold_legacy"
    SYSTEM_SNAPSHOT = "system_snapshot"


REQUIRED_HISTORY_SOURCE_KINDS = frozenset(HistorySourceKind)


class HistorySession(StrictModel):
    session_ref: NonBlank
    source_session_id: NonBlank
    source_kind: HistorySourceKind
    source_id: NonBlank
    message_count: Annotated[int, Field(gt=0)]
    first_message_at: datetime
    last_message_at: datetime

    @model_validator(mode="after")
    def bounds_match_count(self) -> "HistorySession":
        if self.last_message_at < self.first_message_at:
            raise ValueError("HistorySession message bounds are reversed")
        return self


class HistorySourceManifest(StrictModel):
    source_id: NonBlank
    source_kind: HistorySourceKind
    ownership: Literal["personal"]
    owner_id: NonBlank
    record_count: Annotated[int, Field(ge=0)]
    raw_bytes: Annotated[int, Field(ge=0)]
    digest_sha256: Sha256
    cutover_cursor: NonBlank


class HistoryImportReceipt(StrictModel):
    schema: Literal["schema:history-activation-handoff"]
    schema_version: Literal["1.0.0"]
    inventory_id: NonBlank
    data_space_id: Identifier
    manifest_digest: Sha256
    record_count: Annotated[int, Field(ge=0)]
    raw_bytes: Annotated[int, Field(ge=0)]
    cross_source_overlap_identities: Annotated[int, Field(ge=0)]
    sources: tuple[HistorySourceManifest, ...]
    session_count: Annotated[int, Field(ge=0)]
    sessions: tuple[HistorySession, ...]
    session_index_ref: NonBlank
    open_commitments_ref: NonBlank
    current_state_ref: NonBlank

    @model_validator(mode="after")
    def totals_and_coverage_are_consistent(self) -> "HistoryImportReceipt":
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("history source identities must be unique")
        if {
            source.source_kind for source in self.sources
        } != REQUIRED_HISTORY_SOURCE_KINDS:
            raise ValueError(
                "history handoff must contain the exact seven source kinds"
            )
        if sum(source.record_count for source in self.sources) != self.record_count:
            raise ValueError("history handoff record_count differs from sources")
        if sum(source.raw_bytes for source in self.sources) != self.raw_bytes:
            raise ValueError("history handoff raw_bytes differs from sources")
        if len(self.sessions) != self.session_count:
            raise ValueError("history handoff session_count differs from sessions")
        if len({session.session_ref for session in self.sessions}) != len(
            self.sessions
        ):
            raise ValueError("history session references must be unique")
        source_by_id = {source.source_id: source for source in self.sources}
        if any(
            session.source_id not in source_by_id
            or source_by_id[session.source_id].source_kind is not session.source_kind
            for session in self.sessions
        ):
            raise ValueError("history session source provenance is inconsistent")
        return self


class CurrentWorkGraphSnapshot(StrictModel):
    snapshot_id: Identifier
    data_space_id: Identifier
    captured_at: datetime
    nodes: tuple[UVSMNode, ...]
    work_items: tuple[WorkItem, ...]
    edges: tuple[WorkEdge, ...]
    snapshot_sha256: Sha256

    def calculated_sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude={"snapshot_sha256"})
        for node in payload["nodes"]:
            node["resident_functions"] = sorted(node["resident_functions"])
        canonical = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @model_validator(mode="after")
    def identities_and_real_work_are_valid(self) -> "CurrentWorkGraphSnapshot":
        node_ids = [item.node_id for item in self.nodes]
        work_ids = [item.work_item_id for item in self.work_items]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Work Graph snapshot Node identities must be unique")
        if len(work_ids) != len(set(work_ids)):
            raise ValueError("Work Graph snapshot WorkItem identities must be unique")
        if not any(
            item.state not in (WorkState.COMPLETED, WorkState.CANCELLED)
            for item in self.work_items
        ):
            raise ValueError("Work Graph snapshot requires a real incomplete WorkItem")
        if len(self.edges) != len(set(self.edges)):
            raise ValueError("Work Graph snapshot edges must be unique")
        return self


class EvidenceCitation(StrictModel):
    claim_ref: NonBlank
    evidence_ref: Identifier


AssessmentUnderstanding = Annotated[str, Field(min_length=1, max_length=1_200)]
AssessmentItem = Annotated[str, Field(min_length=1, max_length=500)]


class ReorientationAssessment(StrictModel):
    assessment_id: Identifier
    import_id: Identifier
    conversation_id: Identifier
    generated_at: datetime
    understanding: AssessmentUnderstanding
    active_missions: Annotated[
        tuple[AssessmentItem, ...],
        Field(max_length=8),
    ]
    decisions_and_constraints: Annotated[
        tuple[AssessmentItem, ...],
        Field(max_length=12),
    ]
    open_commitment_ids: tuple[Identifier, ...]
    unknowns: Annotated[
        tuple[AssessmentItem, ...],
        Field(max_length=8),
    ]
    resume_work_item_ids: tuple[Identifier, ...]
    covered_session_index_ref: NonBlank
    covered_session_count: Annotated[int, Field(ge=0)]
    history_cursor: Annotated[int, Field(ge=0)]
    current_state_cursor: Annotated[int, Field(ge=0)]
    citations: Annotated[
        tuple[EvidenceCitation, ...],
        Field(max_length=32),
    ]


class ActivationStatus(StrictModel):
    state: ActivationState
    import_receipt: HistoryImportReceipt | None
    assessment: ReorientationAssessment | None
    approved_at: datetime | None
    status_model_calls: Literal[0]
    reorientation_attempt_in_progress: bool = False
    pending_reorientation_revision_reason: ReorientationRevisionReason | None = None
    reorientation_pilot_calls: Annotated[int, Field(ge=0)]
    reorientation_input_tokens: Annotated[int, Field(ge=0)]
    reorientation_output_tokens: Annotated[int, Field(ge=0)]
    reorientation_error: NonBlank | None = None
    work_graph_snapshot_id: Identifier | None = None
    reorientation_conversation_id: Identifier | None = None
