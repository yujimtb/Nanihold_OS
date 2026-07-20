from __future__ import annotations

from dataclasses import dataclass

from vsm.errors import InvariantViolation, ModelMismatch
from vsm.pilot.models import (
    CacheDecision,
    CacheOpportunity,
    PilotMode,
    PilotPolicy,
    PilotRequest,
    PilotResponse,
)


@dataclass(frozen=True)
class ClaudeLaunch:
    argv: tuple[str, ...]
    policy: PilotPolicy
    env: tuple[tuple[str, str], ...]


class ClaudePilotAdapter:
    """Claude Code-specific behavior kept outside the Kernel."""

    adapter_name = "claude-code"

    def __init__(self, *, adapter_version: str, policy: PilotPolicy) -> None:
        if not adapter_version:
            raise InvariantViolation("Claude adapter version is required")
        self.adapter_version = adapter_version
        self.policy = policy

    def build_launch(self, request: PilotRequest) -> ClaudeLaunch:
        candidate = request.requested_candidate
        if candidate.adapter != self.adapter_name:
            raise InvariantViolation("ModelCandidate is not for Claude Code")
        if candidate.adapter_version != self.adapter_version:
            raise InvariantViolation("Claude adapter version mismatch")
        if self.policy.mode is PilotMode.OBSERVE_ONLY and request.effect_capabilities:
            raise InvariantViolation("observe_only rejects write Effect capabilities")
        argv = [
            "claude",
            "--model",
            candidate.model_snapshot,
            "--effort",
            candidate.effort,
        ]
        if request.provider_session_id is not None:
            argv.extend(("--resume", request.provider_session_id))
        if self.policy.mode is PilotMode.SANDBOXED_BYPASS:
            argv.append("--dangerously-skip-permissions")
        return ClaudeLaunch(
            argv=tuple(argv),
            policy=self.policy,
            env=(("CLAUDE_CODE_DISABLE_AUTO_MODEL_SWITCH", "1"),),
        )

    def decide_cache_warming(self, opportunity: CacheOpportunity) -> CacheDecision:
        root = opportunity.root_session
        identity_matches = (
            root.relation == "root"
            and root.model_candidate_key == opportunity.requested_candidate_key
            and root.working_directory_fingerprint
            == opportunity.working_directory_fingerprint
            and root.mcp_prefix_fingerprint == opportunity.mcp_prefix_fingerprint
        )
        if not identity_matches:
            reason = "identity_mismatch"
        elif opportunity.posterior_confidence < 0.95:
            reason = "confidence_below_95_percent"
        elif opportunity.quota_remaining_fraction < opportunity.quota_floor_fraction:
            reason = "quota_below_floor"
        elif opportunity.owner_turn_queued:
            reason = "owner_turn_queued"
        else:
            expected_saving = opportunity.next_use_probability * (
                opportunity.cold_input_cost - opportunity.cache_hit_input_cost
            )
            reason = (
                "expected_saving_exceeds_cost"
                if expected_saving > opportunity.warming_cost
                else "not_economical"
            )
        return CacheDecision(
            warm=reason == "expected_saving_exceeds_cost",
            reason=reason,
            root_session_id=root.root_session_id,
        )

    def build_cache_warm_launch(
        self, opportunity: CacheOpportunity
    ) -> ClaudeLaunch:
        decision = self.decide_cache_warming(opportunity)
        if not decision.warm:
            raise InvariantViolation(f"cache warming rejected: {decision.reason}")
        return ClaudeLaunch(
            argv=(
                "claude",
                "--resume",
                decision.root_session_id,
                "--fork-session",
                "--effort",
                "high",
            ),
            policy=self.policy,
            env=(("CLAUDE_CODE_DISABLE_AUTO_MODEL_SWITCH", "1"),),
        )

    def validate_response(
        self, request: PilotRequest, response: PilotResponse
    ) -> PilotResponse:
        requested = request.requested_candidate
        if response.requested_candidate_key != requested.key:
            raise InvariantViolation("PilotResponse candidate key mismatch")
        if (
            response.actual_provider != requested.provider
            or response.actual_model_snapshot != requested.model_snapshot
        ):
            raise ModelMismatch(
                "RequestedActualModelMismatch: "
                f"requested={requested.provider}/{requested.model_snapshot}, "
                f"actual={response.actual_provider}/{response.actual_model_snapshot}"
            )
        if (
            self.policy.mode is PilotMode.SANDBOXED_BYPASS
            and response.classifier_triggered
        ):
            raise InvariantViolation(
                "permission classifier triggered in sandboxed_bypass mode"
            )
        return response
