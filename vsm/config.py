from __future__ import annotations

import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vsm.errors import ConfigurationError
from vsm.kernel.models import (
    AuditPolicy,
    ControlPolicy,
    DataSpace,
)
from vsm.pilot.models import ModelCandidate, PilotMode, PilotPolicy, SandboxProfile
from vsm.routing.bayesian import BenchmarkPrior


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LetheConfig(StrictConfig):
    base_url: str = Field(min_length=1)
    bearer_token_env: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


class KernelConfig(StrictConfig):
    data_space: DataSpace
    lethe: LetheConfig
    audit_policy: AuditPolicy
    control_policy: ControlPolicy


class SandboxConfig(StrictConfig):
    profile_id: str
    certificate_file: Path
    certificate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filesystem_write_roots: tuple[str, ...]
    network_destinations: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime


class PilotConfig(StrictConfig):
    mode: PilotMode
    permission_classifier_enabled: bool
    writes_allowed: bool
    sandbox: SandboxConfig | None
    pilot_host_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    device_certificate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def policy(self) -> PilotPolicy:
        profile = None
        if self.sandbox is not None:
            if not self.sandbox.certificate_file.is_file():
                raise ConfigurationError(
                    f"SandboxProfile certificate not found: {self.sandbox.certificate_file}"
                )
            import hashlib

            actual = hashlib.sha256(self.sandbox.certificate_file.read_bytes()).hexdigest()
            if actual != self.sandbox.certificate_sha256:
                raise ConfigurationError("SandboxProfile certificate digest mismatch")
            now = datetime.now(UTC)
            issued_at = self.sandbox.issued_at
            expires_at = self.sandbox.expires_at
            if issued_at.tzinfo is None or expires_at.tzinfo is None:
                raise ConfigurationError(
                    "SandboxProfile timestamps must include a timezone"
                )
            if not (issued_at <= now < expires_at):
                raise ConfigurationError(
                    "SandboxProfile certificate is not currently valid"
                )
            profile = SandboxProfile(
                profile_id=self.sandbox.profile_id,
                certificate_sha256=actual,
                filesystem_write_roots=self.sandbox.filesystem_write_roots,
                network_destinations=self.sandbox.network_destinations,
                issued_at=self.sandbox.issued_at,
                expires_at=self.sandbox.expires_at,
            )
        return PilotPolicy(
            mode=self.mode,
            sandbox_profile=profile,
            permission_classifier_enabled=self.permission_classifier_enabled,
            writes_allowed=self.writes_allowed,
        )


class InterfacePilotConfig(StrictConfig):
    node_id: str = Field(min_length=1)
    pilot_id: str = Field(min_length=1)
    adapter: Literal["claude-code"]
    adapter_version: str = Field(min_length=1)
    provider: Literal["anthropic"]
    model_snapshot: Literal["claude-fable-5"]
    effort: Literal["high"]
    toolset: tuple[str, ...]
    sandbox_fingerprint: str = Field(min_length=1)
    environment_fingerprint: str = Field(min_length=1)


class CandidateRegistration(StrictConfig):
    candidate: ModelCandidate
    priors: tuple[BenchmarkPrior, ...]


class RoutingConfig(StrictConfig):
    active_route_snapshot_id: str
    candidates: tuple[CandidateRegistration, ...]
    expected_utility_quality_weight: float = Field(gt=0)
    expected_utility_cost_weight: float = Field(ge=0)
    expected_utility_latency_weight: float = Field(ge=0)
    production_exploration_enabled: Literal[False]


class ServerConfig(StrictConfig):
    bind_host: str = Field(min_length=1)
    bind_port: int = Field(gt=0, le=65535)
    api_bearer_token_env: str = Field(min_length=1)
    allowed_origins: tuple[str, ...]


class NaniholdConfig(StrictConfig):
    kernel: KernelConfig
    pilot: PilotConfig
    interface_pilot: InterfacePilotConfig
    routing: RoutingConfig
    server: ServerConfig

    @model_validator(mode="after")
    def data_space_and_policy_are_consistent(self) -> "NaniholdConfig":
        data_space_id = self.kernel.data_space.data_space_id
        if self.kernel.audit_policy.data_space_id != data_space_id:
            raise ValueError("AuditPolicy DataSpace does not match Kernel DataSpace")
        if self.kernel.control_policy.data_space_id != data_space_id:
            raise ValueError("ControlPolicy DataSpace does not match Kernel DataSpace")
        if not self.routing.candidates:
            raise ValueError("routing candidates must not be empty")
        keys = [item.candidate.key for item in self.routing.candidates]
        if len(keys) != len(set(keys)):
            raise ValueError("routing ModelCandidate keys must be unique")
        for registration in self.routing.candidates:
            for prior in registration.priors:
                if prior.version.startswith("EXAMPLE") or prior.harness.startswith(
                    "EXAMPLE"
                ):
                    raise ValueError(
                        "example benchmark evidence must be replaced before startup"
                    )
        interface = self.interface_pilot
        interface_matches = [
            item.candidate
            for item in self.routing.candidates
            if (
                item.candidate.adapter == interface.adapter
                and item.candidate.adapter_version == interface.adapter_version
                and item.candidate.provider == interface.provider
                and item.candidate.model_snapshot == interface.model_snapshot
                and item.candidate.effort == interface.effort
                and item.candidate.toolset == interface.toolset
                and item.candidate.sandbox_fingerprint
                == interface.sandbox_fingerprint
                and item.candidate.environment_fingerprint
                == interface.environment_fingerprint
            )
        ]
        if len(interface_matches) != 1:
            raise ValueError(
                "the claude-fable-5/high Interface candidate must appear exactly once "
                "in the model registry"
            )
        return self


class LoadedConfig(StrictConfig):
    config: NaniholdConfig
    lethe_bearer_token: str
    api_bearer_token: str


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ConfigurationError(f"required environment variable is missing: {name}")
    return value


def load_config(path: Path) -> LoadedConfig:
    if not path.is_file():
        raise ConfigurationError(f"Nanihold config not found: {path}")
    try:
        config = NaniholdConfig.model_validate(tomllib.loads(path.read_text("utf-8")))
    except Exception as exc:
        raise ConfigurationError(f"invalid Nanihold config: {exc}") from exc
    try:
        from vsm.ids import validate_id

        validate_id(config.routing.active_route_snapshot_id)
    except Exception as exc:
        raise ConfigurationError("active RouteSnapshot ID is invalid") from exc
    pilot_policy = config.pilot.policy()
    if pilot_policy.mode is not config.pilot.mode:
        raise ConfigurationError("Pilot mode changed during validation")
    return LoadedConfig(
        config=config,
        lethe_bearer_token=_required_env(config.kernel.lethe.bearer_token_env),
        api_bearer_token=_required_env(config.server.api_bearer_token_env),
    )
