from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict

from vsm.errors import InvariantViolation, ModelMismatch
from vsm.pilot.models import (
    InterfaceTurn,
    ModelCandidate,
    StructuredInterfaceResponse,
)


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PilotHostInterfaceResponse(_StrictResponse):
    requested_candidate_key: str
    actual_provider: str
    actual_model_snapshot: str
    structured_response: StructuredInterfaceResponse


class PilotHostInterfacePilot:
    """Authenticated Interface RPC to an external, device-owned PilotHost."""

    def __init__(
        self,
        *,
        candidate: ModelCandidate,
        base_url: str,
        bearer_token: str,
        timeout_seconds: float,
    ) -> None:
        if not base_url or not bearer_token or timeout_seconds <= 0:
            raise InvariantViolation(
                "PilotHost connection fields must be explicit and valid"
            )
        self.candidate = candidate
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=timeout_seconds,
        )
        response = self._client.get("/health")
        self._raise(response)
        health = response.json()
        if health.get("candidate_key") != candidate.key:
            self._client.close()
            raise InvariantViolation(
                "PilotHost candidate differs from the configured Interface candidate"
            )

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_success:
            return
        raise InvariantViolation(
            f"PilotHost HTTP {response.status_code}: {response.text}"
        )

    def close(self) -> None:
        self._client.close()

    def respond(
        self, *, owner_text: str, context: InterfaceTurn
    ) -> StructuredInterfaceResponse:
        response = self._client.post(
            "/v1/interface-turn",
            json={
                "candidate": self.candidate.model_dump(
                    mode="json", exclude_computed_fields=True
                ),
                "owner_text": owner_text,
                "context": context.model_dump(mode="json"),
            },
        )
        self._raise(response)
        result = PilotHostInterfaceResponse.model_validate(response.json())
        if result.requested_candidate_key != self.candidate.key:
            raise InvariantViolation("PilotHost response candidate key mismatch")
        if (
            result.actual_provider != self.candidate.provider
            or result.actual_model_snapshot != self.candidate.model_snapshot
        ):
            raise ModelMismatch(
                "RequestedActualModelMismatch: "
                f"requested={self.candidate.provider}/"
                f"{self.candidate.model_snapshot}, "
                f"actual={result.actual_provider}/{result.actual_model_snapshot}"
            )
        return result.structured_response
