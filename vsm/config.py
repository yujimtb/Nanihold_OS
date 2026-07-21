from __future__ import annotations

import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vsm.errors import ConfigurationError, InvariantViolation
from vsm.environment import EnvironmentContract, environment_fingerprint
from vsm.kernel.models import (
    AuditPolicy,
    ControlPolicy,
    DataSpace,
)
from vsm.pilot.models import ModelCandidate, PilotMode, PilotPolicy, SandboxProfile
from vsm.routing.bayesian import BenchmarkPrior, require_coding_escalation_candidates


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LetheConfig(StrictConfig):
    base_url: str = Field(min_length=1)
    bearer_token_env: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    max_page_size: int = Field(gt=0, le=1000)
    history_max_result_bytes: int = Field(gt=0)


class KernelConfig(StrictConfig):
    data_space: DataSpace
    lethe: LetheConfig
    audit_policy: AuditPolicy
    control_policy: ControlPolicy


class DeploymentConfig(StrictConfig):
    mode: Literal["production", "local_verification"]


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
    sandbox: SandboxConfig | None = None
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
    adapter: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model_selection: Literal["exact", "provider_configured"]
    model_snapshot: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    toolset: tuple[str, ...]
    sandbox_fingerprint: str = Field(min_length=1)
    environment_fingerprint: str = Field(min_length=1)
    pilot_host_base_url: str = Field(min_length=1)
    pilot_host_bearer_token_env: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def pilot_host_url_is_explicit_http(self) -> "InterfacePilotConfig":
        parsed = urlparse(self.pilot_host_base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "interface_pilot.pilot_host_base_url must be an explicit HTTP(S) URL"
            )
        if self.model_selection == "exact" and self.model_snapshot is None:
            raise ValueError("exact Interface Pilot requires model_snapshot")
        if (
            self.model_selection == "provider_configured"
            and self.model_snapshot is not None
        ):
            raise ValueError("provider_configured Interface Pilot forbids model_snapshot")
        return self


class ProductionPilotHostRuntimeConfig(StrictConfig):
    coding_pilot_id: str = Field(min_length=1)
    coding_candidate_model_snapshot: str = Field(min_length=1)
    agent_name_csv_path: Path
    interface_max_budget_usd: float = Field(gt=0)
    transport_timeout_seconds: float = Field(gt=0)
    work_cwd: str = Field(min_length=1)
    work_sandbox: Literal["read-only", "workspace-write"]
    work_max_input_tokens: int = Field(gt=0)
    work_max_output_tokens: int = Field(gt=0)
    work_max_total_tokens: int = Field(gt=0)
    work_timeout_seconds: float = Field(gt=0)
    max_parallelism: int = Field(gt=0, le=32)
    reorientation_max_tool_rounds: int = Field(gt=0, le=100)

    @model_validator(mode="after")
    def work_total_covers_parts(self) -> "ProductionPilotHostRuntimeConfig":
        if self.work_max_total_tokens < (
            self.work_max_input_tokens + self.work_max_output_tokens
        ):
            raise ValueError(
                "production_pilot_host.work_max_total_tokens must cover parts"
            )
        return self


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
    authorized_device_ids: tuple[str, ...]
    owner_session_lifetime_seconds: int = Field(gt=0)
    # Owner opt-out for localhost-only deployments. Default False keeps
    # authentication enabled; when True the Bearer + X-Nanihold-Device-Id and
    # owner bootstrap session checks are bypassed. This is an explicit, reversible
    # flag (set back to False to restore auth) — the auth code itself is retained.
    owner_auth_disabled: bool = False

    @model_validator(mode="after")
    def device_identities_are_explicit(self) -> "ServerConfig":
        if (
            not self.authorized_device_ids
            or len(self.authorized_device_ids) != len(set(self.authorized_device_ids))
        ):
            raise ValueError("server.authorized_device_ids must be non-empty and unique")
        return self


class NaniholdConfig(StrictConfig):
    deployment: DeploymentConfig
    kernel: KernelConfig
    pilot: PilotConfig
    interface_pilot: InterfacePilotConfig
    environment_contract: EnvironmentContract | None = None
    production_pilot_host: ProductionPilotHostRuntimeConfig | None = None
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
        if self.environment_contract is not None:
            expected_environment_fingerprint = environment_fingerprint(
                self.environment_contract
            )
            declared_fingerprints = {
                "interface_pilot": self.interface_pilot.environment_fingerprint,
                **{
                    f"routing.candidates[{index}]": registration.candidate.environment_fingerprint
                    for index, registration in enumerate(self.routing.candidates)
                },
            }
            mismatches = [
                location
                for location, declared in declared_fingerprints.items()
                if declared != expected_environment_fingerprint
            ]
            if mismatches:
                raise ConfigurationError(
                    "environment_fingerprint does not match environment_contract: "
                    + ", ".join(mismatches)
                )
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
                and item.candidate.selection == interface.model_selection
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
                "the configured Interface candidate must appear exactly once in the "
                "model registry"
            )
        if self.deployment.mode == "production":
            if self.production_pilot_host is None:
                raise ValueError(
                    "production requires production_pilot_host receipt contract"
                )
            try:
                coding_escalation = require_coding_escalation_candidates(
                    tuple(item.candidate for item in self.routing.candidates)
                )
            except InvariantViolation as exc:
                raise ValueError(str(exc)) from exc
            if (
                self.production_pilot_host.coding_candidate_model_snapshot
                != coding_escalation[0].model_snapshot
            ):
                raise ValueError(
                    "production PilotHost coding candidate must match the first "
                    "coding escalation candidate"
                )
            if any(
                prior.source == "local-verification"
                for registration in self.routing.candidates
                for prior in registration.priors
            ):
                raise ValueError(
                    "production routing cannot use local-verification priors"
                )
            if (
                interface.adapter != "claude-code"
                or interface.provider != "anthropic"
                or interface.model_selection != "provider_configured"
                or interface.model_snapshot is not None
                or interface.effort != "high"
            ):
                raise ValueError(
                    "the production Interface Pilot requires "
                    "claude-code/anthropic/provider_configured/high"
                )
        else:
            if self.production_pilot_host is not None:
                raise ValueError(
                    "local verification cannot configure production_pilot_host"
                )
            if interface.effort != "low":
                raise ValueError("local verification requires Interface effort low")
            if (
                interface.model_selection != "exact"
                or interface.model_snapshot is None
                or interface.model_snapshot
                not in {"claude-haiku-4-5-20251001"}
            ):
                raise ValueError(
                    "local verification requires the approved cheap exact Interface model"
                )
            if self.pilot.mode is not PilotMode.OBSERVE_ONLY:
                raise ValueError("local verification requires observe_only Pilot mode")
            if self.pilot.writes_allowed:
                raise ValueError("local verification cannot allow write Effects")
        return self


class LoadedConfig(StrictConfig):
    config: NaniholdConfig
    lethe_bearer_token: str
    api_bearer_token: str
    pilot_host_bearer_token: str


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
        pilot_host_bearer_token=_required_env(
            config.interface_pilot.pilot_host_bearer_token_env
        ),
    )
