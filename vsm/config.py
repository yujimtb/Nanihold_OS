"""Configuration loader for the VSM PoC platform.

This module provides the two configuration value-objects used by the
runtime — :class:`LLMConfig` and :class:`RunConfig` — together with the
:func:`load_config` helper that parses ``vsm.toml`` (Python 3.11 stdlib
``tomllib``) and merges it with environment variables.

Design references
-----------------
- design.md `## Architecture` §設計の中核方針 #4: provider selection priority
  is environment variable ``LITELLM_PROVIDER`` > ``vsm.toml`` ``[llm]
  provider`` > error (REQ 3.7).
- design.md `## Components and Interfaces` §4: ``LLMConfig.resolve_model``
  returns a LiteLLM-compatible model string such as
  ``"openai/gpt-4o-mini"`` or ``"anthropic/claude-3-5-sonnet"``.
- design.md `## Components and Interfaces` §7: ``RunConfig.systems_for(role)
  -> int`` is the surface used by structural verification (REQ 13.1) to
  check that mandatory roles have at least one configured instance.

Requirements traced
-------------------
- REQ 1.3: 0..1024 S1_Worker instances at startup time.
- REQ 1.4: Each System hosts 1..64 Sub_Agent instances.
- REQ 3.7: Provider selection from ``LITELLM_PROVIDER`` or config file
  without modifying System / Sub_Agent code.
- REQ 13.4: Mandatory Systems (S2..S5, S3*) are configured with 1..16
  Sub_Agent instances at Run start.
- REQ 13.5: S1_Worker count may be zero at Run start.
- REQ 13.6: S3_Allocator may dynamically create up to 64 concurrent
  S1_Worker instances during a Run.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

from vsm.errors import ConfigError, NativeRunDisabledError
from vsm.roles import MANDATORY_ROLES, SystemRole
from vsm.runtime.manifest import DEFAULT_SELFDEV_FORBIDDEN_PATHS

__all__ = [
    "LLMConfig",
    "AgentBackendConfig",
    "AgentsConfig",
    "SessionConfig",
    "CoordinationConfig",
    "AlgedonicConfig",
    "ConsortiumConfig",
    "BudgetConfig",
    "QuotaConfig",
    "ResidencyConfig",
    "SelfDevConfig",
    "LetheConfig",
    "RunConfig",
    "require_native_runs_enabled",
    "load_config",
    "LITELLM_PROVIDER_ENV",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DOTENV_PATH",
    "S1_HARD_MAX",
    "S1_DYNAMIC_MAX",
    "SUB_AGENT_HARD_MAX",
    "MANDATORY_SUB_AGENT_MIN",
    "MANDATORY_SUB_AGENT_MAX",
    "CLAUDE_BIN_ENV",
    "CODEX_BIN_ENV",
    "NANIHOLD_USE_FAKE_LLM_ENV",
]


# Name of the environment variable that, when set, overrides the LLM
# provider selection from any configuration file (REQ 3.7).
LITELLM_PROVIDER_ENV = "LITELLM_PROVIDER"
CLAUDE_BIN_ENV = "CLAUDE_BIN"
CODEX_BIN_ENV = "CODEX_BIN"
NANIHOLD_USE_FAKE_LLM_ENV = "NANIHOLD_USE_FAKE_LLM"

# Default location of the configuration file relative to the current
# working directory. ``vsm.toml`` is the project-local convention used by
# design.md §設計の中核方針 #4.
DEFAULT_CONFIG_PATH = Path("vsm.toml")

# Optional local environment file. ``load_config`` reads this before checking
# OS environment variables so local CLI runs can persist provider credentials.
# python-dotenv does not override variables already set in the shell by default.
DEFAULT_DOTENV_PATH = Path(".env")

# REQ 1.3: at startup time the platform MUST allow between 0 and 1024
# S1_Worker instances (the upper bound is the absolute hard limit).
S1_HARD_MAX = 1024

# REQ 13.6: while a Run is in progress, S3_Allocator may dynamically
# create additional S1_Worker instances up to a configured maximum of 64
# concurrent instances.
S1_DYNAMIC_MAX = 64

# REQ 1.4: every System SHALL host between 1 and 64 Sub_Agent instances.
SUB_AGENT_HARD_MAX = 64

# REQ 13.4: each mandatory System (S2..S5, S3*) is configured with 1..16
# Sub_Agent instances at Run start time.
MANDATORY_SUB_AGENT_MIN = 1
MANDATORY_SUB_AGENT_MAX = 16


# ---------------------------------------------------------------------------
# LLMConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMConfig:
    """Resolved LLM provider configuration (REQ 3.7).

    Two source layers are tracked separately so that
    :meth:`resolve_model` can apply the documented priority order without
    losing diagnostic information:

    Attributes
    ----------
    provider_from_env : str | None
        The value of the ``LITELLM_PROVIDER`` environment variable at
        construction time, or ``None`` if it was unset / empty.
    provider_from_file : str | None
        The value of ``[llm] provider`` from ``vsm.toml`` if present,
        otherwise ``None``.
    model_overrides : Mapping[str, str]
        Optional per-System model overrides, keyed by ``SystemRole.value``
        (e.g. ``"S4_SCANNER"``). The mapping is stored as a plain
        :class:`dict` and treated as read-only after construction; it is
        intentionally permissive so that future tasks can plumb richer
        per-Sub_Agent overrides without breaking this signature.

    Notes
    -----
    The class is frozen to discourage accidental mutation while a Run is
    active. The selection priority enforced by :meth:`resolve_model` is
    *env > file > error*, matching design.md §4 and REQ 3.7.
    """

    provider_from_env: str | None = None
    provider_from_file: str | None = None
    model_overrides: Mapping[str, str] = field(default_factory=dict)

    def resolve_model(self, role: SystemRole | None = None) -> str:
        """Return the LiteLLM-compatible model string for ``role``.

        Resolution priority (REQ 3.7, design.md §4):

        1. The environment variable ``LITELLM_PROVIDER`` if it is set to a
           non-empty string.
        2. The ``[llm] provider`` entry from ``vsm.toml`` if it is set to
           a non-empty string.
        3. Otherwise raise :class:`ConfigError`.

        Parameters
        ----------
        role : SystemRole | None
            If provided and the role has an entry in :attr:`model_overrides`,
            the override value is returned regardless of env / file
            settings. This is intentional: per-System overrides are
            explicit caller intent and supersede the global default.

        Returns
        -------
        str
            A LiteLLM-compatible model identifier such as
            ``"openai/gpt-4o-mini"`` or ``"anthropic/claude-3-5-sonnet"``.

        Raises
        ------
        ConfigError
            When no override applies and neither the environment variable
            nor the configuration file specifies a provider.
        """
        if role is not None:
            override = self.model_overrides.get(role.value)
            if override:
                return override

        if self.provider_from_env:
            return self.provider_from_env
        if self.provider_from_file:
            return self.provider_from_file

        raise ConfigError(
            missing_roles=[],
            detail=(
                "LLM provider is not configured: set the "
                f"{LITELLM_PROVIDER_ENV!s} environment variable or define "
                "[llm] provider = \"<model>\" in vsm.toml (REQ 3.7)"
            ),
        )


# ---------------------------------------------------------------------------
# RunConfig
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentBackendConfig:
    """単一 AgentRuntime バックエンドの設定。"""

    bin: str | None
    model: str
    timeout_seconds: float
    reasoning_effort: str | None = None
    timeout_explicit: bool = True

    def __post_init__(self) -> None:
        if self.bin is not None and (not isinstance(self.bin, str) or not self.bin.strip()):
            raise ConfigError(missing_roles=[], detail="agent backend bin must not be empty")
        if not isinstance(self.model, str):
            raise ConfigError(missing_roles=[], detail="agent backend model must be a string")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
        ):
            raise ConfigError(
                missing_roles=[],
                detail="agent backend timeout_seconds must be positive",
            )
        if not isinstance(self.timeout_explicit, bool):
            raise ConfigError(
                missing_roles=[], detail="agent backend timeout_explicit must be a boolean"
            )
        if self.reasoning_effort is not None and (
            not isinstance(self.reasoning_effort, str) or not self.reasoning_effort.strip()
        ):
            raise ConfigError(
                missing_roles=[], detail="agent backend reasoning_effort must not be empty"
            )


def _default_agent_backends() -> dict[str, AgentBackendConfig]:
    return {
        "claude-code": AgentBackendConfig(
            bin="claude", model="", timeout_seconds=1800.0, timeout_explicit=False
        ),
        "codex": AgentBackendConfig(
            bin="codex",
            model="gpt-5.6-sol",
            reasoning_effort="high",
            timeout_seconds=1800.0,
            timeout_explicit=False,
        ),
        "litellm": AgentBackendConfig(
            bin=None, model="", timeout_seconds=60.0, timeout_explicit=False
        ),
        "fake": AgentBackendConfig(
            bin=None, model="fake/test-model", timeout_seconds=60.0, timeout_explicit=False
        ),
    }


def _default_agent_roles() -> dict[SystemRole, str]:
    return {
        SystemRole.S5_POLICY: "claude-code",
        SystemRole.S4_SCANNER: "claude-code",
        SystemRole.S3_ALLOCATOR: "",
        SystemRole.S2_COORDINATOR: "claude-code",
        SystemRole.S3STAR_AUDITOR: "claude-code",
        SystemRole.S1_WORKER: "codex",
    }


@dataclass(frozen=True)
class AgentsConfig:
    """ロール別 AgentRuntime 解決設定。"""

    default_backend: str = "claude-code"
    backends: Mapping[str, AgentBackendConfig] = field(
        default_factory=_default_agent_backends
    )
    roles: Mapping[SystemRole, str] = field(default_factory=_default_agent_roles)

    def __post_init__(self) -> None:
        backends = dict(self.backends)
        roles = dict(self.roles)
        if not isinstance(self.default_backend, str) or not self.default_backend:
            raise ConfigError(
                missing_roles=[], detail="agents.default_backend must not be empty"
            )
        expected_backends = {"claude-code", "codex", "litellm", "fake"}
        if set(backends) != expected_backends:
            raise ConfigError(
                missing_roles=[],
                detail=(
                    "agents.backends must define exactly "
                    f"{sorted(expected_backends)}, got {sorted(backends)}"
                ),
            )
        if backends["claude-code"].bin is None:
            raise ConfigError(missing_roles=[], detail="claude-code bin is required")
        codex = backends["codex"]
        if codex.bin is None or not codex.model or codex.reasoning_effort is None:
            raise ConfigError(
                missing_roles=[],
                detail="codex bin, model, and reasoning_effort are required",
            )
        if self.default_backend not in backends:
            raise ConfigError(
                missing_roles=[],
                detail=f"unknown agents.default_backend: {self.default_backend!r}",
            )
        for role, backend in roles.items():
            if not isinstance(role, SystemRole):
                raise ConfigError(
                    missing_roles=[], detail=f"invalid agents.roles key: {role!r}"
                )
            if not isinstance(backend, str):
                raise ConfigError(
                    missing_roles=[role.value],
                    detail=f"backend for role {role.value} must be a string",
                )
            if backend and backend not in backends:
                raise ConfigError(
                    missing_roles=[role.value],
                    detail=f"unknown backend {backend!r} for role {role.value}",
                )
        object.__setattr__(self, "backends", backends)
        object.__setattr__(self, "roles", roles)

    def backend_for(self, role: SystemRole) -> str | None:
        """ロールに割り当てたバックエンド名を返す。空文字は未割当。"""

        value = self.roles.get(role, self.default_backend)
        return value or None


@dataclass(frozen=True)
class SessionConfig:
    """CLI セッションの利用範囲設定。"""

    resume_within_run: bool = True


@dataclass(frozen=True)
class CoordinationConfig:
    """S2 の調停判断に AgentRuntime を使うかを制御する。"""

    ai_deliberation: bool = True


@dataclass(frozen=True)
class AlgedonicConfig:
    """Algedonic signal の人間向け通知設定。"""

    notify_human: bool = True


@dataclass(frozen=True)
class ConsortiumConfig:
    """階層非依存 Consortium のプロトコル設定。"""

    default_rounds: int = 2
    human_participation: str = "invited"
    human_timeout_seconds: float = 3600.0
    human_timeout_policy: str = "proceed"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.default_rounds, int)
            or isinstance(self.default_rounds, bool)
            or self.default_rounds < 1
        ):
            raise ConfigError(
                missing_roles=[], detail="consortium.default_rounds must be a positive integer"
            )
        if self.human_participation not in {"invited", "required", "none"}:
            raise ConfigError(
                missing_roles=[],
                detail="consortium.human_participation must be invited, required, or none",
            )
        if (
            not isinstance(self.human_timeout_seconds, (int, float))
            or isinstance(self.human_timeout_seconds, bool)
            or self.human_timeout_seconds <= 0
        ):
            raise ConfigError(
                missing_roles=[], detail="consortium.human_timeout_seconds must be positive"
            )
        if self.human_timeout_policy not in {"proceed", "abort"}:
            raise ConfigError(
                missing_roles=[],
                detail="consortium.human_timeout_policy must be proceed or abort",
            )


@dataclass(frozen=True)
class BudgetConfig:
    """Run 全体とロール別の AgentRuntime 予算。"""

    run_tokens: int = 2_000_000
    run_wall_clock_seconds: float = 7_200.0
    invocation_initial_tokens: int = 4_096
    invocation_initial_wall_clock_seconds: float = 60.0
    invocation_safety_multiplier: float = 1.25
    roles: Mapping[SystemRole, Mapping[str, float]] = field(
        default_factory=lambda: {
            SystemRole.S1_WORKER: {
                "tokens": 500_000.0,
                "wall_clock_seconds": 1_800.0,
            }
        }
    )

    def __post_init__(self) -> None:
        if not isinstance(self.run_tokens, int) or isinstance(self.run_tokens, bool) or self.run_tokens <= 0:
            raise ConfigError(missing_roles=[], detail="budget.run_tokens must be a positive integer")
        if (
            not isinstance(self.run_wall_clock_seconds, (int, float))
            or isinstance(self.run_wall_clock_seconds, bool)
            or self.run_wall_clock_seconds <= 0
        ):
            raise ConfigError(
                missing_roles=[], detail="budget.run_wall_clock_seconds must be positive"
            )
        if (
            not isinstance(self.invocation_initial_tokens, int)
            or isinstance(self.invocation_initial_tokens, bool)
            or self.invocation_initial_tokens <= 0
        ):
            raise ConfigError(
                missing_roles=[],
                detail="budget.invocation_initial_tokens must be a positive integer",
            )
        if (
            not isinstance(self.invocation_initial_wall_clock_seconds, (int, float))
            or isinstance(self.invocation_initial_wall_clock_seconds, bool)
            or self.invocation_initial_wall_clock_seconds <= 0
        ):
            raise ConfigError(
                missing_roles=[],
                detail="budget.invocation_initial_wall_clock_seconds must be positive",
            )
        if (
            not isinstance(self.invocation_safety_multiplier, (int, float))
            or isinstance(self.invocation_safety_multiplier, bool)
            or self.invocation_safety_multiplier < 1
        ):
            raise ConfigError(
                missing_roles=[],
                detail="budget.invocation_safety_multiplier must be at least 1",
            )
        normalised: dict[SystemRole, dict[str, float]] = {}
        for role, envelope in self.roles.items():
            if not isinstance(role, SystemRole) or not isinstance(envelope, Mapping):
                raise ConfigError(missing_roles=[], detail="budget.roles must map SystemRole to a table")
            if set(envelope) != {"tokens", "wall_clock_seconds"}:
                raise ConfigError(
                    missing_roles=[role.value],
                    detail=f"budget.roles.{role.value} must define tokens and wall_clock_seconds",
                )
            values = {key: value for key, value in envelope.items()}
            for key, value in values.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                    raise ConfigError(
                        missing_roles=[role.value],
                        detail=f"budget.roles.{role.value}.{key} must be positive",
                    )
            normalised[role] = {key: float(value) for key, value in values.items()}
        object.__setattr__(self, "roles", normalised)

    def envelope_for(self, role: SystemRole) -> dict[str, float]:
        return dict(
            self.roles.get(
                role,
                {
                    "tokens": float(self.run_tokens),
                    "wall_clock_seconds": float(self.run_wall_clock_seconds),
                },
            )
        )


@dataclass(frozen=True)
class QuotaConfig:
    """サブスクリプション quota 枯渇時の休眠・復帰設定。"""

    suspend_on_exhausted: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.suspend_on_exhausted, bool):
            raise ConfigError(missing_roles=[], detail="quota.suspend_on_exhausted must be a boolean")


@dataclass(frozen=True)
class SelfDevConfig:
    """self-hosting Run の有効化と repository 保護設定。"""

    enabled: bool = False
    repository: Path = field(default_factory=lambda: Path.cwd())
    forbidden_paths: tuple[str, ...] = DEFAULT_SELFDEV_FORBIDDEN_PATHS
    implementation_timeout_margin_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError(missing_roles=[], detail="selfdev.enabled must be a boolean")
        if not isinstance(self.repository, Path):
            raise ConfigError(
                missing_roles=[], detail="selfdev.repository must be a path string"
            )
        if any(not isinstance(value, str) or not value.strip() for value in self.forbidden_paths):
            raise ConfigError(
                missing_roles=[], detail="selfdev.forbidden_paths must contain non-empty strings"
            )
        if (
            not isinstance(self.implementation_timeout_margin_seconds, (int, float))
            or isinstance(self.implementation_timeout_margin_seconds, bool)
            or self.implementation_timeout_margin_seconds < 0
        ):
            raise ConfigError(
                missing_roles=[],
                detail="selfdev.implementation_timeout_margin_seconds must be non-negative",
            )
        object.__setattr__(self, "repository", self.repository.resolve(strict=False))
        object.__setattr__(self, "forbidden_paths", tuple(self.forbidden_paths))


@dataclass(frozen=True)
class ResidencyConfig:
    """恒常稼働時の native Run 起動可否。"""

    native_runs_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.native_runs_enabled, bool):
            raise ConfigError(
                missing_roles=[],
                detail="residency.native_runs_enabled must be a boolean",
            )


@dataclass(frozen=True)
class LetheConfig:
    """Run 間の会計・長期記憶を LETHE へ接続する設定。"""

    enabled: bool = False
    mode: str = "dry-run"
    dry_run_path: Path = field(
        default_factory=lambda: Path("runs") / "lethe-dry-run.jsonl"
    )
    endpoint: str | None = None
    token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigError(missing_roles=[], detail="lethe.enabled must be a boolean")
        if self.mode not in {"dry-run", "live"}:
            raise ConfigError(
                missing_roles=[], detail="lethe.mode must be dry-run or live"
            )
        if not isinstance(self.dry_run_path, Path):
            raise ConfigError(
                missing_roles=[], detail="lethe.dry_run_path must be a path string"
            )
        for name in ("endpoint", "token"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ConfigError(
                    missing_roles=[], detail=f"lethe.{name} must be a non-empty string"
                )
        if self.enabled and self.mode == "live":
            if self.endpoint is None:
                raise ConfigError(
                    missing_roles=[], detail="enabled live LETHE requires lethe.endpoint"
                )
            if self.token is None:
                raise ConfigError(
                    missing_roles=[], detail="enabled live LETHE requires lethe.token"
                )
            parsed = urlsplit(self.endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.query
                or parsed.fragment
            ):
                raise ConfigError(
                    missing_roles=[],
                    detail=(
                        "lethe.endpoint must be an absolute http(s) URL "
                        "without query or fragment"
                    ),
                )


def _validate_sub_agent_count(role: SystemRole, count: int) -> None:
    """Validate a Sub_Agent count for a single role.

    The bounds depend on whether the role is mandatory:

    * Mandatory roles (REQ 13.4): ``1 <= count <= 16``.
    * S1_WORKER (REQ 13.5, 1.4): ``0 <= count <= 64`` at Run start time.
      The dynamic concurrent ceiling of 64 (REQ 13.6) is enforced
      separately by S3_Allocator at runtime.

    Raises
    ------
    ConfigError
        With ``missing_roles=[role.value]`` when the count is rejected.
    """
    if not isinstance(count, int) or isinstance(count, bool):
        raise ConfigError(
            missing_roles=[role.value],
            detail=(
                f"Sub_Agent count for {role.value} must be an int, "
                f"got {type(count).__name__}"
            ),
        )

    if role is SystemRole.S1_WORKER:
        # REQ 13.5: zero is allowed at Run start.
        # REQ 1.4 / 13.6: per-System upper bound is 64.
        if count < 0 or count > SUB_AGENT_HARD_MAX:
            raise ConfigError(
                missing_roles=[role.value],
                detail=(
                    f"S1_WORKER startup count must be in [0, "
                    f"{SUB_AGENT_HARD_MAX}], got {count} "
                    "(REQ 13.5, 13.6, 1.4)"
                ),
            )
        return

    # Mandatory roles: REQ 13.4 enforces a tighter [1, 16] range.
    if count < MANDATORY_SUB_AGENT_MIN or count > MANDATORY_SUB_AGENT_MAX:
        raise ConfigError(
            missing_roles=[role.value],
            detail=(
                f"Sub_Agent count for {role.value} must be in "
                f"[{MANDATORY_SUB_AGENT_MIN}, {MANDATORY_SUB_AGENT_MAX}], "
                f"got {count} (REQ 13.4, 1.4)"
            ),
        )


@dataclass(frozen=True)
class RunConfig:
    """Per-Run configuration of System / Sub_Agent counts (REQ 13).

    Attributes
    ----------
    sub_agents_per_role : Mapping[SystemRole, int]
        Number of Sub_Agent instances each System will host at Run start.
        Defaults to one Sub_Agent for every mandatory role and zero
        S1_Worker instances (which is the minimal valid Run-start
        configuration per REQ 13.4 + 13.5).
    s1_max : int
        Hard upper bound on the *total* number of S1_Worker instances
        that may exist during a Run (REQ 1.3 sets the absolute ceiling
        at 1024). The smaller dynamic concurrent ceiling — 64 — is
        defined by REQ 13.6 and is exposed as :attr:`s1_dynamic_max`.
    s1_dynamic_max : int
        Maximum number of concurrent S1_Worker instances that
        S3_Allocator may dynamically create (REQ 13.6). Defaults to
        :data:`S1_DYNAMIC_MAX`.

    Construction-time validation (REQ 13.4, 13.5, 13.6, 1.3, 1.4) raises
    :class:`ConfigError` for any out-of-range value. Validation is done
    in ``__post_init__`` so that callers cannot bypass it via the
    dataclass constructor.

    Use :meth:`count` (or its alias :meth:`systems_for`) for read-only
    access to the configured Sub_Agent count of each role; both are
    accepted by the lifecycle structural verifier (design.md §7).
    """

    sub_agents_per_role: Mapping[SystemRole, int] = field(
        default_factory=lambda: {
            SystemRole.S1_WORKER: 0,
            SystemRole.S2_COORDINATOR: 1,
            SystemRole.S3_ALLOCATOR: 1,
            SystemRole.S3STAR_AUDITOR: 1,
            SystemRole.S4_SCANNER: 1,
            SystemRole.S5_POLICY: 1,
        }
    )
    s1_max: int = S1_HARD_MAX
    s1_dynamic_max: int = S1_DYNAMIC_MAX
    agents: AgentsConfig = field(default_factory=AgentsConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    coordination: CoordinationConfig = field(default_factory=CoordinationConfig)
    algedonic: AlgedonicConfig = field(default_factory=AlgedonicConfig)
    consortium: ConsortiumConfig = field(default_factory=ConsortiumConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    selfdev: SelfDevConfig = field(default_factory=SelfDevConfig)
    residency: ResidencyConfig = field(default_factory=ResidencyConfig)
    lethe: LetheConfig = field(default_factory=LetheConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.agents, AgentsConfig):
            raise ConfigError(
                missing_roles=[], detail="RunConfig.agents must be an AgentsConfig"
            )
        if not isinstance(self.session, SessionConfig):
            raise ConfigError(
                missing_roles=[], detail="RunConfig.session must be a SessionConfig"
            )
        if not isinstance(self.coordination, CoordinationConfig):
            raise ConfigError(
                missing_roles=[], detail="RunConfig.coordination must be a CoordinationConfig"
            )
        if not isinstance(self.algedonic, AlgedonicConfig):
            raise ConfigError(
                missing_roles=[], detail="RunConfig.algedonic must be an AlgedonicConfig"
            )
        if not isinstance(self.consortium, ConsortiumConfig):
            raise ConfigError(
                missing_roles=[], detail="RunConfig.consortium must be a ConsortiumConfig"
            )
        if not isinstance(self.budget, BudgetConfig):
            raise ConfigError(missing_roles=[], detail="RunConfig.budget must be a BudgetConfig")
        if not isinstance(self.quota, QuotaConfig):
            raise ConfigError(missing_roles=[], detail="RunConfig.quota must be a QuotaConfig")
        if not isinstance(self.selfdev, SelfDevConfig):
            raise ConfigError(missing_roles=[], detail="RunConfig.selfdev must be a SelfDevConfig")
        if not isinstance(self.residency, ResidencyConfig):
            raise ConfigError(
                missing_roles=[], detail="RunConfig.residency must be a ResidencyConfig"
            )
        if not isinstance(self.lethe, LetheConfig):
            raise ConfigError(missing_roles=[], detail="RunConfig.lethe must be a LetheConfig")
        # Materialise the mapping into a plain dict so that callers can
        # not mutate the configuration after construction. The frozen
        # dataclass would still allow mutation through the original
        # mapping reference; copying defends against that.
        normalised: dict[SystemRole, int] = {}
        for role, count in self.sub_agents_per_role.items():
            if not isinstance(role, SystemRole):
                raise ConfigError(
                    missing_roles=[],
                    detail=(
                        "RunConfig.sub_agents_per_role keys must be "
                        f"SystemRole values, got {role!r}"
                    ),
                )
            _validate_sub_agent_count(role, count)
            normalised[role] = count
        # Bypass the frozen-dataclass guard once during initialisation.
        object.__setattr__(self, "sub_agents_per_role", normalised)

        # REQ 1.3: absolute S1 ceiling is 1024.
        if (
            not isinstance(self.s1_max, int)
            or isinstance(self.s1_max, bool)
            or self.s1_max < 0
            or self.s1_max > S1_HARD_MAX
        ):
            raise ConfigError(
                missing_roles=[],
                detail=(
                    f"s1_max must be an int in [0, {S1_HARD_MAX}], "
                    f"got {self.s1_max!r} (REQ 1.3)"
                ),
            )

        # REQ 13.6: dynamic concurrent S1 ceiling is 64.
        if (
            not isinstance(self.s1_dynamic_max, int)
            or isinstance(self.s1_dynamic_max, bool)
            or self.s1_dynamic_max < 0
            or self.s1_dynamic_max > S1_DYNAMIC_MAX
        ):
            raise ConfigError(
                missing_roles=[],
                detail=(
                    f"s1_dynamic_max must be an int in [0, "
                    f"{S1_DYNAMIC_MAX}], got {self.s1_dynamic_max!r} "
                    "(REQ 13.6)"
                ),
            )
        if self.s1_dynamic_max > self.s1_max:
            raise ConfigError(
                missing_roles=[],
                detail=(
                    f"s1_dynamic_max ({self.s1_dynamic_max}) must not "
                    f"exceed s1_max ({self.s1_max}) (REQ 1.3, 13.6)"
                ),
            )

    def count(self, role: SystemRole) -> int:
        """Return the configured Sub_Agent count for ``role``.

        Returns ``0`` when ``role`` is absent from the mapping; this is
        meaningful only for :attr:`SystemRole.S1_WORKER` (REQ 13.5). For
        mandatory roles the structural verifier (REQ 13.1) will reject a
        zero count downstream.
        """
        return self.sub_agents_per_role.get(role, 0)

    def systems_for(self, role: SystemRole) -> int:
        """Alias of :meth:`count`.

        Provided to match the API surface used by design.md §7
        (``config.systems_for(role)``) so that the lifecycle module can
        read the configured count using either name interchangeably.
        """
        return self.count(role)


def require_native_runs_enabled(run_config: RunConfig) -> None:
    """native Run の起動・再開を許可し、封鎖中なら即時に失敗させる。"""

    if not run_config.residency.native_runs_enabled:
        raise NativeRunDisabledError()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _read_env_provider() -> str | None:
    """Return the ``LITELLM_PROVIDER`` environment value, or ``None`` if unset.

    Empty strings and whitespace-only values are normalised to ``None`` so
    that they do not satisfy the env-priority branch in
    :meth:`LLMConfig.resolve_model`.
    """
    raw = os.environ.get(LITELLM_PROVIDER_ENV)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _parse_toml(path: Path) -> dict[str, Any]:
    """Read and parse a TOML file using the stdlib ``tomllib`` module.

    Raises
    ------
    ConfigError
        When the file is unreadable or contains invalid TOML.
    """
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(
            missing_roles=[],
            detail=f"failed to read configuration file {path}: {exc}",
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            missing_roles=[],
            detail=f"invalid TOML in {path}: {exc}",
        ) from exc


def _extract_llm_section(raw: Mapping[str, Any], path: Path) -> tuple[str | None, dict[str, str]]:
    """Extract ``[llm]`` provider and per-System overrides from a TOML mapping.

    Returns
    -------
    tuple[str | None, dict[str, str]]
        A pair ``(provider, overrides)``. ``provider`` is the
        ``[llm] provider`` value or ``None`` if absent / empty.
        ``overrides`` maps ``SystemRole.value`` to a model string and is
        empty when no overrides are defined.
    """
    section = raw.get("llm")
    if section is None:
        return None, {}
    if not isinstance(section, Mapping):
        raise ConfigError(
            missing_roles=[],
            detail=f"[llm] section in {path} must be a table",
        )

    provider_raw = section.get("provider")
    provider: str | None
    if provider_raw is None:
        provider = None
    elif isinstance(provider_raw, str):
        stripped = provider_raw.strip()
        provider = stripped or None
    else:
        raise ConfigError(
            missing_roles=[],
            detail=(
                f"[llm] provider in {path} must be a string, got "
                f"{type(provider_raw).__name__}"
            ),
        )

    overrides: dict[str, str] = {}
    raw_overrides = section.get("model_overrides")
    if raw_overrides is not None:
        if not isinstance(raw_overrides, Mapping):
            raise ConfigError(
                missing_roles=[],
                detail=(
                    f"[llm.model_overrides] in {path} must be a table, "
                    f"got {type(raw_overrides).__name__}"
                ),
            )
        valid_role_values = {role.value for role in SystemRole}
        for key, value in raw_overrides.items():
            if key not in valid_role_values:
                raise ConfigError(
                    missing_roles=[],
                    detail=(
                        f"[llm.model_overrides] key {key!r} in {path} is "
                        f"not a valid SystemRole "
                        f"(expected one of {sorted(valid_role_values)})"
                    ),
                )
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(
                    missing_roles=[],
                    detail=(
                        f"[llm.model_overrides.{key}] in {path} must be "
                        "a non-empty string"
                    ),
                )
            overrides[key] = value.strip()
    return provider, overrides


def _extract_run_section(
    raw: Mapping[str, Any],
    path: Path,
    *,
    agents: AgentsConfig,
    session: SessionConfig,
    coordination: CoordinationConfig,
    algedonic: AlgedonicConfig,
    consortium: ConsortiumConfig,
    budget: BudgetConfig,
    quota: QuotaConfig,
    residency: ResidencyConfig,
    selfdev: SelfDevConfig,
    lethe: LetheConfig,
) -> RunConfig:
    """Build a :class:`RunConfig` from the optional ``[run]`` TOML section.

    Recognised entries (all optional):

    * ``sub_agents`` — table of role-name -> int. Missing keys fall back
      to the :class:`RunConfig` default values.
    * ``s1_max`` — integer (REQ 1.3 ceiling).
    * ``s1_dynamic_max`` — integer (REQ 13.6 ceiling).
    """
    section = raw.get("run")
    if section is None:
        return RunConfig(
            agents=agents,
            session=session,
            coordination=coordination,
            algedonic=algedonic,
            consortium=consortium,
            budget=budget,
            quota=quota,
            residency=residency,
            selfdev=selfdev,
            lethe=lethe,
        )
    if not isinstance(section, Mapping):
        raise ConfigError(
            missing_roles=[],
            detail=f"[run] section in {path} must be a table",
        )

    # Start from the dataclass defaults, then layer in TOML values.
    base = RunConfig()
    counts: dict[SystemRole, int] = dict(base.sub_agents_per_role)

    raw_counts = section.get("sub_agents")
    if raw_counts is not None:
        if not isinstance(raw_counts, Mapping):
            raise ConfigError(
                missing_roles=[],
                detail=(
                    f"[run.sub_agents] in {path} must be a table, "
                    f"got {type(raw_counts).__name__}"
                ),
            )
        valid_role_values = {role.value: role for role in SystemRole}
        for key, value in raw_counts.items():
            if key not in valid_role_values:
                raise ConfigError(
                    missing_roles=[],
                    detail=(
                        f"[run.sub_agents] key {key!r} in {path} is not a "
                        f"valid SystemRole "
                        f"(expected one of {sorted(valid_role_values)})"
                    ),
                )
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigError(
                    missing_roles=[valid_role_values[key].value],
                    detail=(
                        f"[run.sub_agents.{key}] in {path} must be an int, "
                        f"got {type(value).__name__}"
                    ),
                )
            counts[valid_role_values[key]] = value

    s1_max = section.get("s1_max", base.s1_max)
    s1_dynamic_max = section.get("s1_dynamic_max", base.s1_dynamic_max)

    return RunConfig(
        sub_agents_per_role=counts,
        s1_max=s1_max,
        s1_dynamic_max=s1_dynamic_max,
        agents=agents,
        session=session,
        coordination=coordination,
        algedonic=algedonic,
        consortium=consortium,
        budget=budget,
        quota=quota,
        residency=residency,
        selfdev=selfdev,
        lethe=lethe,
    )


def _require_string(value: Any, *, field_name: str, path: Path) -> str:
    if not isinstance(value, str):
        raise ConfigError(
            missing_roles=[],
            detail=f"{field_name} in {path} must be a string",
        )
    return value.strip()


def _extract_agents_section(raw: Mapping[str, Any], path: Path) -> AgentsConfig:
    """``[agents]`` と CLI bin の環境変数上書きを読み込む。"""

    defaults = AgentsConfig()
    section = raw.get("agents")
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ConfigError(missing_roles=[], detail=f"[agents] section in {path} must be a table")

    default_backend = _require_string(
        section.get("default_backend", defaults.default_backend),
        field_name="[agents] default_backend",
        path=path,
    )
    if not default_backend:
        raise ConfigError(missing_roles=[], detail="[agents] default_backend must not be empty")

    backends = dict(defaults.backends)
    raw_backends = section.get("backends", {})
    if not isinstance(raw_backends, Mapping):
        raise ConfigError(missing_roles=[], detail=f"[agents.backends] in {path} must be a table")
    unknown_backends = set(raw_backends) - set(backends)
    if unknown_backends:
        raise ConfigError(
            missing_roles=[],
            detail=f"unknown [agents.backends] entries: {sorted(unknown_backends)}",
        )
    for name, raw_backend in raw_backends.items():
        if not isinstance(raw_backend, Mapping):
            raise ConfigError(
                missing_roles=[], detail=f"[agents.backends.{name}] in {path} must be a table"
            )
        base = backends[name]
        allowed = {"bin", "model", "timeout_seconds", "reasoning_effort"}
        unknown_fields = set(raw_backend) - allowed
        if unknown_fields:
            raise ConfigError(
                missing_roles=[],
                detail=f"unknown fields in [agents.backends.{name}]: {sorted(unknown_fields)}",
            )
        bin_value = base.bin
        if "bin" in raw_backend:
            bin_value = _require_string(
                raw_backend["bin"], field_name=f"[agents.backends.{name}] bin", path=path
            )
            if not bin_value:
                raise ConfigError(
                    missing_roles=[], detail=f"[agents.backends.{name}] bin must not be empty"
                )
        model = _require_string(
            raw_backend.get("model", base.model),
            field_name=f"[agents.backends.{name}] model",
            path=path,
        )
        timeout_seconds = raw_backend.get("timeout_seconds", base.timeout_seconds)
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ConfigError(
                missing_roles=[],
                detail=f"[agents.backends.{name}] timeout_seconds must be positive",
            )
        effort = base.reasoning_effort
        if "reasoning_effort" in raw_backend:
            effort = _require_string(
                raw_backend["reasoning_effort"],
                field_name=f"[agents.backends.{name}] reasoning_effort",
                path=path,
            )
            if not effort:
                raise ConfigError(
                    missing_roles=[],
                    detail=f"[agents.backends.{name}] reasoning_effort must not be empty",
                )
        backends[name] = AgentBackendConfig(
            bin=bin_value,
            model=model,
            timeout_seconds=float(timeout_seconds),
            reasoning_effort=effort,
            timeout_explicit=("timeout_seconds" in raw_backend) or base.timeout_explicit,
        )

    raw_roles = section.get("roles", {})
    if not isinstance(raw_roles, Mapping):
        raise ConfigError(missing_roles=[], detail=f"[agents.roles] in {path} must be a table")
    roles = dict(defaults.roles)
    roles_by_name = {role.value: role for role in SystemRole}
    for role_name, backend in raw_roles.items():
        role = roles_by_name.get(role_name)
        if role is None:
            raise ConfigError(
                missing_roles=[], detail=f"unknown [agents.roles] role: {role_name!r}"
            )
        roles[role] = _require_string(
            backend, field_name=f"[agents.roles] {role_name}", path=path
        )

    for env_name, backend_name in (
        (CLAUDE_BIN_ENV, "claude-code"),
        (CODEX_BIN_ENV, "codex"),
    ):
        env_value = os.environ.get(env_name)
        if env_value is None:
            continue
        resolved = env_value.strip()
        if not resolved:
            raise ConfigError(missing_roles=[], detail=f"{env_name} must not be empty")
        base = backends[backend_name]
        backends[backend_name] = AgentBackendConfig(
            bin=resolved,
            model=base.model,
            timeout_seconds=base.timeout_seconds,
            reasoning_effort=base.reasoning_effort,
        )
    return AgentsConfig(
        default_backend=default_backend,
        backends=backends,
        roles=roles,
    )


def _extract_session_section(raw: Mapping[str, Any], path: Path) -> SessionConfig:
    section = raw.get("session")
    if section is None:
        return SessionConfig()
    if not isinstance(section, Mapping):
        raise ConfigError(missing_roles=[], detail=f"[session] section in {path} must be a table")
    unknown = set(section) - {"resume_within_run"}
    if unknown:
        raise ConfigError(missing_roles=[], detail=f"unknown [session] fields: {sorted(unknown)}")
    value = section.get("resume_within_run", True)
    if not isinstance(value, bool):
        raise ConfigError(
            missing_roles=[], detail="[session] resume_within_run must be a boolean"
        )
    return SessionConfig(resume_within_run=value)


def _extract_residency_section(
    raw: Mapping[str, Any], path: Path
) -> ResidencyConfig:
    """``[residency]`` の native Run 起動許可を読み込む。"""

    section = raw.get("residency")
    if section is None:
        return ResidencyConfig()
    if not isinstance(section, Mapping):
        raise ConfigError(
            missing_roles=[], detail=f"[residency] section in {path} must be a table"
        )
    unknown = set(section) - {"native_runs_enabled"}
    if unknown:
        raise ConfigError(
            missing_roles=[],
            detail=f"unknown [residency] fields: {sorted(unknown)}",
        )
    value = section.get("native_runs_enabled", False)
    if not isinstance(value, bool):
        raise ConfigError(
            missing_roles=[],
            detail="[residency] native_runs_enabled must be a boolean",
        )
    return ResidencyConfig(native_runs_enabled=value)


def _extract_boolean_section(
    raw: Mapping[str, Any],
    path: Path,
    *,
    section_name: str,
    field_name: str,
    default: bool,
) -> bool:
    section = raw.get(section_name)
    if section is None:
        return default
    if not isinstance(section, Mapping):
        raise ConfigError(
            missing_roles=[], detail=f"[{section_name}] section in {path} must be a table"
        )
    unknown = set(section) - {field_name}
    if unknown:
        raise ConfigError(
            missing_roles=[], detail=f"unknown [{section_name}] fields: {sorted(unknown)}"
        )
    value = section.get(field_name, default)
    if not isinstance(value, bool):
        raise ConfigError(
            missing_roles=[], detail=f"[{section_name}] {field_name} must be a boolean"
        )
    return value


def _extract_consortium_section(raw: Mapping[str, Any], path: Path) -> ConsortiumConfig:
    section = raw.get("consortium")
    if section is None:
        return ConsortiumConfig()
    if not isinstance(section, Mapping):
        raise ConfigError(
            missing_roles=[], detail=f"[consortium] section in {path} must be a table"
        )
    allowed = {
        "default_rounds",
        "human_participation",
        "human_timeout_seconds",
        "human_timeout_policy",
    }
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(
            missing_roles=[], detail=f"unknown [consortium] fields: {sorted(unknown)}"
        )
    defaults = ConsortiumConfig()
    return ConsortiumConfig(
        default_rounds=section.get("default_rounds", defaults.default_rounds),
        human_participation=_require_string(
            section.get("human_participation", defaults.human_participation),
            field_name="[consortium] human_participation",
            path=path,
        ),
        human_timeout_seconds=section.get(
            "human_timeout_seconds", defaults.human_timeout_seconds
        ),
        human_timeout_policy=_require_string(
            section.get("human_timeout_policy", defaults.human_timeout_policy),
            field_name="[consortium] human_timeout_policy",
            path=path,
        ),
    )


def _extract_budget_section(raw: Mapping[str, Any], path: Path) -> BudgetConfig:
    section = raw.get("budget")
    if section is None:
        return BudgetConfig()
    if not isinstance(section, Mapping):
        raise ConfigError(missing_roles=[], detail=f"[budget] section in {path} must be a table")
    allowed = {
        "run_tokens",
        "run_wall_clock_seconds",
        "invocation_initial_tokens",
        "invocation_initial_wall_clock_seconds",
        "invocation_safety_multiplier",
        "roles",
    }
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(missing_roles=[], detail=f"unknown [budget] fields: {sorted(unknown)}")
    defaults = BudgetConfig()
    raw_roles = section.get("roles", defaults.roles)
    if not isinstance(raw_roles, Mapping):
        raise ConfigError(missing_roles=[], detail=f"[budget.roles] in {path} must be a table")
    roles_by_name = {role.value: role for role in SystemRole}
    roles: dict[SystemRole, Mapping[str, float]] = {}
    for name, envelope in raw_roles.items():
        role = roles_by_name.get(name) if isinstance(name, str) else name
        if role not in roles_by_name.values():
            raise ConfigError(missing_roles=[], detail=f"unknown [budget.roles] role: {name!r}")
        roles[role] = envelope
    return BudgetConfig(
        run_tokens=section.get("run_tokens", defaults.run_tokens),
        run_wall_clock_seconds=section.get(
            "run_wall_clock_seconds", defaults.run_wall_clock_seconds
        ),
        invocation_initial_tokens=section.get(
            "invocation_initial_tokens", defaults.invocation_initial_tokens
        ),
        invocation_initial_wall_clock_seconds=section.get(
            "invocation_initial_wall_clock_seconds",
            defaults.invocation_initial_wall_clock_seconds,
        ),
        invocation_safety_multiplier=section.get(
            "invocation_safety_multiplier", defaults.invocation_safety_multiplier
        ),
        roles=roles,
    )


def _extract_quota_section(raw: Mapping[str, Any], path: Path) -> QuotaConfig:
    section = raw.get("quota")
    if section is None:
        return QuotaConfig()
    if not isinstance(section, Mapping):
        raise ConfigError(missing_roles=[], detail=f"[quota] section in {path} must be a table")
    allowed = {"suspend_on_exhausted"}
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(missing_roles=[], detail=f"unknown [quota] fields: {sorted(unknown)}")
    defaults = QuotaConfig()
    return QuotaConfig(**{name: section.get(name, getattr(defaults, name)) for name in allowed})


def _apply_explicit_fake_backend(run_config: RunConfig) -> RunConfig:
    """明示された fake 用環境変数を ``[agents]`` の解決結果へ反映する。

    fake はデモ用の暗黙フォールバックではない。環境変数が明示的に
    有効化された場合だけ、もともと AgentRuntime を持つロールを fake に
    置き換える。空のロールは決定論処理のまま維持する。
    """

    value = os.environ.get(NANIHOLD_USE_FAKE_LLM_ENV)
    if value is None or value.lower() not in {"1", "true", "yes"}:
        return run_config

    roles = {
        role: "fake" if run_config.agents.backend_for(role) is not None else ""
        for role in SystemRole
    }
    agents = AgentsConfig(
        default_backend="fake",
        backends=run_config.agents.backends,
        roles=roles,
    )
    return replace(run_config, agents=agents)


def _extract_selfdev_section(raw: Mapping[str, Any], path: Path) -> SelfDevConfig:
    """``[selfdev]`` の self-hosting 境界設定を読み込む。"""

    section = raw.get("selfdev")
    defaults = SelfDevConfig()
    if section is None:
        return defaults
    if not isinstance(section, Mapping):
        raise ConfigError(missing_roles=[], detail=f"[selfdev] section in {path} must be a table")
    allowed = {
        "enabled",
        "repository",
        "forbidden_paths",
        "implementation_timeout_margin_seconds",
    }
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(missing_roles=[], detail=f"unknown [selfdev] fields: {sorted(unknown)}")
    enabled = section.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise ConfigError(missing_roles=[], detail="[selfdev] enabled must be a boolean")
    repository_raw = section.get("repository")
    if repository_raw is None:
        repository = defaults.repository
    elif isinstance(repository_raw, str) and repository_raw.strip():
        repository = Path(repository_raw.strip())
        if not repository.is_absolute():
            repository = path.parent / repository
    else:
        raise ConfigError(missing_roles=[], detail="[selfdev] repository must be a non-empty string")
    raw_forbidden = section.get("forbidden_paths")
    if raw_forbidden is None:
        # 省略時は既定の protected paths(tuple)をそのまま使う。
        raw_forbidden = list(defaults.forbidden_paths)
    if not isinstance(raw_forbidden, list) or any(
        not isinstance(value, str) or not value.strip() for value in raw_forbidden
    ):
        raise ConfigError(
            missing_roles=[], detail="[selfdev] forbidden_paths must be an array of non-empty strings"
        )
    timeout_margin = section.get(
        "implementation_timeout_margin_seconds",
        defaults.implementation_timeout_margin_seconds,
    )
    if (
        not isinstance(timeout_margin, (int, float))
        or isinstance(timeout_margin, bool)
        or timeout_margin < 0
    ):
        raise ConfigError(
            missing_roles=[],
            detail="[selfdev] implementation_timeout_margin_seconds must be non-negative",
        )
    return SelfDevConfig(
        enabled=enabled,
        repository=repository,
        forbidden_paths=tuple(raw_forbidden),
        implementation_timeout_margin_seconds=float(timeout_margin),
    )


def _extract_lethe_section(raw: Mapping[str, Any], path: Path) -> LetheConfig:
    """``[lethe]`` の明示的な接続設定を読み込む。"""

    section = raw.get("lethe")
    defaults = LetheConfig()
    if section is None:
        return defaults
    if not isinstance(section, Mapping):
        raise ConfigError(
            missing_roles=[], detail=f"[lethe] section in {path} must be a table"
        )
    allowed = {"enabled", "mode", "dry_run_path", "endpoint", "token"}
    unknown = set(section) - allowed
    if unknown:
        raise ConfigError(
            missing_roles=[], detail=f"unknown [lethe] fields: {sorted(unknown)}"
        )
    enabled = section.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise ConfigError(missing_roles=[], detail="[lethe] enabled must be a boolean")
    mode = _require_string(
        section.get("mode", defaults.mode),
        field_name="[lethe] mode",
        path=path,
    )
    dry_run_path_raw = section.get("dry_run_path")
    if dry_run_path_raw is None:
        dry_run_path = defaults.dry_run_path
    elif isinstance(dry_run_path_raw, str) and dry_run_path_raw.strip():
        dry_run_path = Path(dry_run_path_raw.strip())
        if not dry_run_path.is_absolute():
            dry_run_path = path.parent / dry_run_path
    else:
        raise ConfigError(
            missing_roles=[], detail="[lethe] dry_run_path must be a non-empty string"
        )

    def optional_string(name: str) -> str | None:
        value = section.get(name)
        if value is None:
            return None
        return _require_string(
            value,
            field_name=f"[lethe] {name}",
            path=path,
        )

    return LetheConfig(
        enabled=enabled,
        mode=mode,
        dry_run_path=dry_run_path,
        endpoint=optional_string("endpoint"),
        token=optional_string("token"),
    )


def load_config(path: Path | None = None) -> tuple[LLMConfig, RunConfig]:
    """Load :class:`LLMConfig` and :class:`RunConfig` from disk + environment.

    Behaviour
    ---------
    * If ``path`` is ``None``, the loader looks for ``vsm.toml`` in the
      current working directory. When that file does not exist, the
      function returns a default :class:`RunConfig` and an
      :class:`LLMConfig` populated only from the environment variable.
      :meth:`LLMConfig.resolve_model` will then raise :class:`ConfigError`
      iff ``LITELLM_PROVIDER`` is also unset (REQ 3.7).
    * If ``path`` is provided but does not exist on disk, that is treated
      as a hard error (the caller asked for a specific file).

    Parameters
    ----------
    path : Path | None
        Explicit path to a ``vsm.toml`` file, or ``None`` to use the
        default location :data:`DEFAULT_CONFIG_PATH`.

    Returns
    -------
    tuple[LLMConfig, RunConfig]
        The resolved configuration objects.

    Raises
    ------
    ConfigError
        When the explicit ``path`` does not exist, when the file cannot
        be read or parsed, when the file contains structurally invalid
        sections, or when validation of the contained values fails.
    """
    dotenv_path = (
        DEFAULT_DOTENV_PATH
        if path is None
        else path.parent / DEFAULT_DOTENV_PATH
    )
    load_dotenv(dotenv_path)
    env_provider = _read_env_provider()

    if path is None:
        candidate = DEFAULT_CONFIG_PATH
        if not candidate.exists():
            agents = _extract_agents_section({}, candidate)
            run_config = _apply_explicit_fake_backend(RunConfig(agents=agents))
            return (
                LLMConfig(provider_from_env=env_provider),
                run_config,
            )
        toml_path = candidate
    else:
        if not path.exists():
            raise ConfigError(
                missing_roles=[],
                detail=f"configuration file does not exist: {path}",
            )
        toml_path = path

    raw = _parse_toml(toml_path)

    file_provider, overrides = _extract_llm_section(raw, toml_path)
    agents = _extract_agents_section(raw, toml_path)
    session = _extract_session_section(raw, toml_path)
    residency = _extract_residency_section(raw, toml_path)
    coordination = CoordinationConfig(
        ai_deliberation=_extract_boolean_section(
            raw,
            toml_path,
            section_name="coordination",
            field_name="ai_deliberation",
            default=True,
        )
    )
    algedonic = AlgedonicConfig(
        notify_human=_extract_boolean_section(
            raw,
            toml_path,
            section_name="algedonic",
            field_name="notify_human",
            default=True,
        )
    )
    consortium = _extract_consortium_section(raw, toml_path)
    budget = _extract_budget_section(raw, toml_path)
    quota = _extract_quota_section(raw, toml_path)
    selfdev = _extract_selfdev_section(raw, toml_path)
    lethe = _extract_lethe_section(raw, toml_path)
    run_config = _extract_run_section(
        raw,
        toml_path,
        agents=agents,
        session=session,
        coordination=coordination,
        algedonic=algedonic,
        consortium=consortium,
        budget=budget,
        quota=quota,
        residency=residency,
        selfdev=selfdev,
        lethe=lethe,
    )
    llm_config = LLMConfig(
        provider_from_env=env_provider,
        provider_from_file=file_provider,
        model_overrides=overrides,
    )
    return llm_config, _apply_explicit_fake_backend(run_config)
