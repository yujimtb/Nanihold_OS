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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vsm.errors import ConfigError
from vsm.roles import MANDATORY_ROLES, SystemRole

__all__ = [
    "LLMConfig",
    "RunConfig",
    "load_config",
    "LITELLM_PROVIDER_ENV",
    "DEFAULT_CONFIG_PATH",
    "S1_HARD_MAX",
    "S1_DYNAMIC_MAX",
    "SUB_AGENT_HARD_MAX",
    "MANDATORY_SUB_AGENT_MIN",
    "MANDATORY_SUB_AGENT_MAX",
]


# Name of the environment variable that, when set, overrides the LLM
# provider selection from any configuration file (REQ 3.7).
LITELLM_PROVIDER_ENV = "LITELLM_PROVIDER"

# Default location of the configuration file relative to the current
# working directory. ``vsm.toml`` is the project-local convention used by
# design.md §設計の中核方針 #4.
DEFAULT_CONFIG_PATH = Path("vsm.toml")

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

    def __post_init__(self) -> None:
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


def _extract_run_section(raw: Mapping[str, Any], path: Path) -> RunConfig:
    """Build a :class:`RunConfig` from the optional ``[run]`` TOML section.

    Recognised entries (all optional):

    * ``sub_agents`` — table of role-name -> int. Missing keys fall back
      to the :class:`RunConfig` default values.
    * ``s1_max`` — integer (REQ 1.3 ceiling).
    * ``s1_dynamic_max`` — integer (REQ 13.6 ceiling).
    """
    section = raw.get("run")
    if section is None:
        return RunConfig()
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
    env_provider = _read_env_provider()

    if path is None:
        candidate = DEFAULT_CONFIG_PATH
        if not candidate.exists():
            return (
                LLMConfig(provider_from_env=env_provider),
                RunConfig(),
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
    run_config = _extract_run_section(raw, toml_path)
    llm_config = LLMConfig(
        provider_from_env=env_provider,
        provider_from_file=file_provider,
        model_overrides=overrides,
    )
    return llm_config, run_config
