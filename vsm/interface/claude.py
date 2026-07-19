from __future__ import annotations

import json
import subprocess

from vsm.errors import InvariantViolation, ModelMismatch
from vsm.pilot.models import (
    InterfaceTurn,
    ModelCandidate,
    PilotMode,
    PilotPolicy,
    StructuredInterfaceResponse,
)


class ClaudeInterfacePilot:
    """Single-call Fable interface; all structured outputs come from one response."""

    def __init__(
        self,
        *,
        candidate: ModelCandidate,
        policy: PilotPolicy,
        timeout_seconds: float,
    ) -> None:
        if candidate.adapter != "claude-code":
            raise InvariantViolation("Interface candidate must use claude-code")
        if candidate.model_snapshot != "claude-fable-5" or candidate.effort != "high":
            raise InvariantViolation(
                "current Interface Pilot default is claude-fable-5/high"
            )
        if timeout_seconds <= 0:
            raise InvariantViolation("Interface Pilot timeout must be positive")
        self.candidate = candidate
        self.policy = policy
        self.timeout_seconds = timeout_seconds

    def respond(
        self, *, owner_text: str, context: InterfaceTurn
    ) -> StructuredInterfaceResponse:
        prompt = json.dumps(
            {
                "instruction": (
                    "Respond as the current Interface Pilot. Return one JSON object with "
                    "display_text, work_directives, decisions, commitment_updates. "
                    "Do not summarize in a second pass."
                ),
                "owner_text": owner_text,
                "context": context.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
        argv = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self.candidate.model_snapshot,
            "--effort",
            self.candidate.effort,
        ]
        if context.provider_session_id is not None:
            argv.extend(("--resume", context.provider_session_id))
        if self.policy.mode is PilotMode.SANDBOXED_BYPASS:
            argv.append("--dangerously-skip-permissions")
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
        )
        if completed.returncode != 0:
            raise InvariantViolation(
                f"Interface Pilot failed with exit {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        try:
            outer = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise InvariantViolation("Interface Pilot returned invalid CLI JSON") from exc
        actual_model = outer.get("model")
        if actual_model != self.candidate.model_snapshot:
            raise ModelMismatch(
                "RequestedActualModelMismatch: "
                f"requested={self.candidate.model_snapshot}, actual={actual_model}"
            )
        result = outer.get("result")
        if not isinstance(result, str):
            raise InvariantViolation("Interface Pilot result is missing")
        try:
            structured = json.loads(result)
        except json.JSONDecodeError as exc:
            raise InvariantViolation(
                "Interface Pilot result is not structured JSON"
            ) from exc
        structured["provider_session_id"] = outer.get("session_id")
        return StructuredInterfaceResponse.model_validate(structured)
