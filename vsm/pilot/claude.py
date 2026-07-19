from __future__ import annotations

from dataclasses import dataclass

from vsm.errors import InvariantViolation, ModelMismatch
from vsm.pilot.models import (
    ModelCandidate,
    PilotMode,
    PilotPolicy,
    PilotRequest,
    PilotResponse,
)


@dataclass(frozen=True)
class ClaudeLaunch:
    argv: tuple[str, ...]
    policy: PilotPolicy


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
        return ClaudeLaunch(argv=tuple(argv), policy=self.policy)

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
