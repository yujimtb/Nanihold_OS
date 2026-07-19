from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from vsm.kernel.models import BlobRef, Identifier, NonBlank


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PilotMode(StrEnum):
    SANDBOXED_BYPASS = "sandboxed_bypass"
    MANAGED_PERMISSIONS = "managed_permissions"
    OBSERVE_ONLY = "observe_only"


class SandboxProfile(StrictModel):
    profile_id: Identifier
    certificate_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    filesystem_write_roots: tuple[NonBlank, ...]
    network_destinations: tuple[NonBlank, ...]
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def valid_interval(self) -> "SandboxProfile":
        if self.expires_at <= self.issued_at:
            raise ValueError("SandboxProfile expires_at must follow issued_at")
        return self


class PilotPolicy(StrictModel):
    mode: PilotMode
    sandbox_profile: SandboxProfile | None
    permission_classifier_enabled: bool
    writes_allowed: bool

    @model_validator(mode="after")
    def mode_isolated(self) -> "PilotPolicy":
        if self.mode is PilotMode.SANDBOXED_BYPASS:
            if self.sandbox_profile is None:
                raise ValueError("sandboxed_bypass requires SandboxProfile evidence")
            if self.permission_classifier_enabled:
                raise ValueError("sandboxed_bypass must not use the permission classifier")
        elif self.mode is PilotMode.MANAGED_PERMISSIONS:
            if not self.permission_classifier_enabled:
                raise ValueError("managed_permissions requires the permission classifier")
        elif self.mode is PilotMode.OBSERVE_ONLY and self.writes_allowed:
            raise ValueError("observe_only cannot permit write effects")
        return self


class ModelCandidate(StrictModel):
    adapter: NonBlank
    adapter_version: NonBlank
    provider: NonBlank
    model_snapshot: NonBlank
    effort: NonBlank
    toolset: tuple[NonBlank, ...]
    sandbox_fingerprint: NonBlank
    environment_fingerprint: NonBlank

    @model_validator(mode="after")
    def toolset_is_nonempty_and_unique(self) -> "ModelCandidate":
        if not self.toolset or len(self.toolset) != len(set(self.toolset)):
            raise ValueError("ModelCandidate toolset must be non-empty and unique")
        return self

    @computed_field
    @property
    def key(self) -> str:
        canonical = json.dumps(
            {
                "adapter": self.adapter,
                "adapter_version": self.adapter_version,
                "provider": self.provider,
                "model_snapshot": self.model_snapshot,
                "effort": self.effort,
                "toolset": sorted(self.toolset),
                "sandbox_fingerprint": self.sandbox_fingerprint,
                "environment_fingerprint": self.environment_fingerprint,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{self.adapter}@{self.adapter_version}:{digest}"


class PilotRequest(StrictModel):
    execution_id: Identifier
    work_item_id: Identifier
    requested_candidate: ModelCandidate
    prompt: NonBlank
    provider_session_id: NonBlank | None
    effect_capabilities: frozenset[NonBlank]


class PilotResponse(StrictModel):
    execution_id: Identifier
    requested_candidate_key: NonBlank
    actual_provider: NonBlank
    actual_model_snapshot: NonBlank
    provider_session_id: NonBlank
    text: NonBlank
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    latency_ms: Annotated[int, Field(ge=0)]
    classifier_triggered: bool
    permission_rejections: Annotated[int, Field(ge=0)]
    reedited_tokens: Annotated[int, Field(ge=0)]


class DeviceIdentity(StrictModel):
    pilot_host_id: Identifier
    device_id: Identifier
    certificate_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PilotHostState(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


class PilotHostStatus(StrictModel):
    identity: DeviceIdentity
    state: PilotHostState
    acknowledged_cursor: Annotated[int, Field(ge=0)]
    connected_at: datetime | None
    disconnected_at: datetime | None


class HandoffPack(StrictModel):
    work_item_id: Identifier
    unmet_acceptance: tuple[NonBlank, ...]
    gate_differences: tuple[NonBlank, ...]
    artifact_refs: tuple[Identifier, ...]
    decision_refs: tuple[Identifier, ...]


class EventDeltaSummary(StrictModel):
    after_cursor: Annotated[int, Field(ge=0)]
    through_cursor: Annotated[int, Field(ge=0)]
    event_count: Annotated[int, Field(ge=0)]
    event_type_counts: dict[str, int]
    changed_stream_ids: tuple[Identifier, ...]


class InterfaceResumePack(StrictModel):
    node_memory: tuple[dict[str, object], ...]
    unfinished_work_items: tuple[dict[str, object], ...]
    open_commitments: tuple[dict[str, object], ...]
    active_decisions: tuple[dict[str, object], ...]


class InterfaceTurn(StrictModel):
    owner_message_blob_ref: BlobRef
    event_delta: EventDeltaSummary
    resume_pack: InterfaceResumePack | None
    provider_session_id: NonBlank | None


class StructuredInterfaceResponse(StrictModel):
    display_text: NonBlank
    work_directives: tuple[dict[str, object], ...]
    decisions: tuple[dict[str, object], ...]
    commitment_updates: tuple[dict[str, object], ...]
    provider_session_id: NonBlank


class JudgeKind(StrEnum):
    DETERMINISTIC = "deterministic"
    CHEAP_AI = "cheap_ai"
    HUMAN = "human"


class JudgeObservation(StrictModel):
    candidate_key: NonBlank
    kind: JudgeKind
    predicted_success: bool
    verified_success: bool | None
    judge_model: NonBlank | None
    judge_effort: Literal["low"] | None

    @model_validator(mode="after")
    def cheap_judge_is_restricted(self) -> "JudgeObservation":
        if self.kind is JudgeKind.CHEAP_AI:
            if self.judge_model is None or self.judge_effort != "low":
                raise ValueError("cheap AI Judge requires an explicit model at low effort")
            lowered = self.judge_model.lower()
            if "fable" in lowered or "opus" in lowered:
                raise ValueError("Fable and Opus are forbidden in Token Efficiency Lab")
        elif self.judge_model is not None or self.judge_effort is not None:
            raise ValueError(
                "deterministic and human Judges cannot declare a model"
            )
        return self
