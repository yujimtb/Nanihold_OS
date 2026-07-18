"""Unit tests for :mod:`vsm.config`.

This module exercises the configuration loader and value-objects defined
in ``vsm/config.py``. The tests cover three concerns:

1. :meth:`LLMConfig.resolve_model` priority — environment variable
   ``LITELLM_PROVIDER`` takes precedence over the ``[llm] provider``
   entry in ``vsm.toml``; when neither source is set, a
   :class:`ConfigError` is raised.
2. :class:`RunConfig` construction-time validation — Sub_Agent counts,
   ``s1_max`` and ``s1_dynamic_max`` boundaries are enforced via
   ``__post_init__``.
3. :func:`load_config` behaviour — default Run directory with neither
   environment variable nor ``vsm.toml`` present yields safe defaults,
   while an explicit non-existent path is a hard error.

Validates Requirements
----------------------
- REQ 1.3: 0..1024 S1_Worker instances at startup time.
- REQ 1.4: each System hosts 1..64 Sub_Agent instances.
- REQ 3.7: provider selection priority ``LITELLM_PROVIDER`` >
  ``vsm.toml`` ``[llm].provider`` > error.
- REQ 13.4: mandatory Systems are configured with 1..16 Sub_Agent
  instances at Run start.
- REQ 13.5: S1_Worker count may be zero at Run start.
- REQ 13.6: S3_Allocator may dynamically create up to 64 concurrent
  S1_Worker instances during a Run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vsm.config import (
    LITELLM_PROVIDER_ENV,
    LLMConfig,
    ResidencyConfig,
    RunConfig,
    load_config,
)
from vsm.errors import ConfigError
from vsm.roles import SystemRole


# ---------------------------------------------------------------------------
# LLMConfig.resolve_model — provider selection priority (REQ 3.7)
# ---------------------------------------------------------------------------


class TestResolveModelPriority:
    """REQ 3.7: env var > vsm.toml > error."""

    def test_env_var_wins_over_file_provider(self, monkeypatch):
        """``LITELLM_PROVIDER`` overrides ``[llm].provider`` from file."""
        monkeypatch.setenv(LITELLM_PROVIDER_ENV, "openai/test")
        cfg = LLMConfig(
            provider_from_env="openai/test",
            provider_from_file="anthropic/claude-3-5-sonnet",
        )

        assert cfg.resolve_model() == "openai/test"

    def test_file_provider_used_when_env_unset(self, monkeypatch):
        """Falls back to ``provider_from_file`` when env value is absent."""
        monkeypatch.delenv(LITELLM_PROVIDER_ENV, raising=False)
        cfg = LLMConfig(
            provider_from_env=None,
            provider_from_file="anthropic/claude-3-5-sonnet",
        )

        assert cfg.resolve_model() == "anthropic/claude-3-5-sonnet"

    def test_raises_when_neither_source_configured(self, monkeypatch):
        """No env, no file → :class:`ConfigError` (REQ 3.7)."""
        monkeypatch.delenv(LITELLM_PROVIDER_ENV, raising=False)
        cfg = LLMConfig(provider_from_env=None, provider_from_file=None)

        with pytest.raises(ConfigError):
            cfg.resolve_model()

    def test_role_override_supersedes_env_and_file(self, monkeypatch):
        """A per-role override takes precedence over both layers."""
        monkeypatch.setenv(LITELLM_PROVIDER_ENV, "openai/test")
        cfg = LLMConfig(
            provider_from_env="openai/test",
            provider_from_file="anthropic/claude-3-5-sonnet",
            model_overrides={"S4_SCANNER": "openai/gpt-4o-mini"},
        )

        assert cfg.resolve_model(SystemRole.S4_SCANNER) == "openai/gpt-4o-mini"
        # Roles without an override still follow the env-priority path.
        assert cfg.resolve_model(SystemRole.S5_POLICY) == "openai/test"


# ---------------------------------------------------------------------------
# RunConfig validation — Sub_Agent counts and S1 ceilings
# ---------------------------------------------------------------------------


class TestRunConfigDefaults:
    """The dataclass defaults satisfy REQ 13.4 / 13.5 / 1.3 / 13.6."""

    def test_default_construction_succeeds(self):
        """``RunConfig()`` with no arguments must pass validation."""
        cfg = RunConfig()

        # Mandatory roles default to one Sub_Agent each (REQ 13.4 lower
        # bound). S1_WORKER defaults to zero (REQ 13.5).
        assert cfg.count(SystemRole.S2_COORDINATOR) == 1
        assert cfg.count(SystemRole.S3_ALLOCATOR) == 1
        assert cfg.count(SystemRole.S3STAR_AUDITOR) == 1
        assert cfg.count(SystemRole.S4_SCANNER) == 1
        assert cfg.count(SystemRole.S5_POLICY) == 1
        assert cfg.count(SystemRole.S1_WORKER) == 0

    def test_count_and_systems_for_are_aliases(self):
        """``systems_for`` mirrors ``count`` for every role (design.md §7)."""
        cfg = RunConfig()

        for role in SystemRole:
            assert cfg.systems_for(role) == cfg.count(role)


class TestRunConfigSubAgentBounds:
    """REQ 13.4 (mandatory 1..16) and REQ 1.4 / 13.6 (S1 0..64)."""

    @pytest.mark.parametrize(
        "role",
        [
            SystemRole.S2_COORDINATOR,
            SystemRole.S3_ALLOCATOR,
            SystemRole.S3STAR_AUDITOR,
            SystemRole.S4_SCANNER,
            SystemRole.S5_POLICY,
        ],
    )
    def test_zero_sub_agents_for_mandatory_role_raises(self, role):
        """REQ 13.4: mandatory roles must have at least one Sub_Agent."""
        counts = {r: 1 for r in SystemRole if r is not SystemRole.S1_WORKER}
        counts[SystemRole.S1_WORKER] = 0
        counts[role] = 0

        with pytest.raises(ConfigError) as excinfo:
            RunConfig(sub_agents_per_role=counts)
        assert role.value in excinfo.value.missing_roles

    @pytest.mark.parametrize(
        "role",
        [
            SystemRole.S2_COORDINATOR,
            SystemRole.S3_ALLOCATOR,
            SystemRole.S3STAR_AUDITOR,
            SystemRole.S4_SCANNER,
            SystemRole.S5_POLICY,
        ],
    )
    def test_seventeen_sub_agents_for_mandatory_role_raises(self, role):
        """REQ 13.4: mandatory roles cap at 16 Sub_Agent instances."""
        counts = {r: 1 for r in SystemRole if r is not SystemRole.S1_WORKER}
        counts[SystemRole.S1_WORKER] = 0
        counts[role] = 17

        with pytest.raises(ConfigError) as excinfo:
            RunConfig(sub_agents_per_role=counts)
        assert role.value in excinfo.value.missing_roles

    def test_sixteen_sub_agents_for_mandatory_role_passes(self):
        """REQ 13.4 upper bound (16) is inclusive."""
        counts = {r: 1 for r in SystemRole if r is not SystemRole.S1_WORKER}
        counts[SystemRole.S1_WORKER] = 0
        counts[SystemRole.S2_COORDINATOR] = 16

        cfg = RunConfig(sub_agents_per_role=counts)
        assert cfg.count(SystemRole.S2_COORDINATOR) == 16

    def test_s1_count_sixty_five_raises(self):
        """REQ 1.4 / 13.6: per-System S1 cap is 64."""
        counts = {r: 1 for r in SystemRole if r is not SystemRole.S1_WORKER}
        counts[SystemRole.S1_WORKER] = 65

        with pytest.raises(ConfigError) as excinfo:
            RunConfig(sub_agents_per_role=counts)
        assert SystemRole.S1_WORKER.value in excinfo.value.missing_roles

    def test_s1_count_zero_is_valid_at_run_start(self):
        """REQ 13.5: S1 may be zero at Run start time."""
        counts = {r: 1 for r in SystemRole if r is not SystemRole.S1_WORKER}
        counts[SystemRole.S1_WORKER] = 0

        cfg = RunConfig(sub_agents_per_role=counts)
        assert cfg.count(SystemRole.S1_WORKER) == 0

    def test_s1_count_sixty_four_is_valid(self):
        """REQ 1.4 upper bound (64) is inclusive for S1_WORKER too."""
        counts = {r: 1 for r in SystemRole if r is not SystemRole.S1_WORKER}
        counts[SystemRole.S1_WORKER] = 64

        cfg = RunConfig(sub_agents_per_role=counts)
        assert cfg.count(SystemRole.S1_WORKER) == 64


class TestRunConfigS1Ceilings:
    """REQ 1.3 (s1_max ≤ 1024) and REQ 13.6 (s1_dynamic_max ≤ 64)."""

    def test_s1_max_above_hard_limit_raises(self):
        """REQ 1.3: ``s1_max = 1025`` exceeds the absolute ceiling."""
        with pytest.raises(ConfigError):
            RunConfig(s1_max=1025)

    def test_s1_max_at_hard_limit_passes(self):
        """REQ 1.3: ``s1_max = 1024`` is inclusive."""
        cfg = RunConfig(s1_max=1024)
        assert cfg.s1_max == 1024

    def test_s1_dynamic_max_above_concurrent_limit_raises(self):
        """REQ 13.6: ``s1_dynamic_max = 65`` exceeds the concurrent cap."""
        with pytest.raises(ConfigError):
            RunConfig(s1_dynamic_max=65)

    def test_s1_dynamic_max_exceeding_s1_max_raises(self):
        """``s1_dynamic_max`` must not be greater than ``s1_max``."""
        # Use values that are individually valid but inconsistent.
        with pytest.raises(ConfigError):
            RunConfig(s1_max=10, s1_dynamic_max=50)


# ---------------------------------------------------------------------------
# RunConfig.count / systems_for accessors
# ---------------------------------------------------------------------------


class TestRunConfigAccessors:
    """``count`` and ``systems_for`` return the configured Sub_Agent count."""

    def test_count_returns_configured_value(self):
        """``count(role)`` matches the value supplied at construction."""
        counts = {r: 1 for r in SystemRole if r is not SystemRole.S1_WORKER}
        counts[SystemRole.S1_WORKER] = 0
        counts[SystemRole.S2_COORDINATOR] = 5

        cfg = RunConfig(sub_agents_per_role=counts)
        assert cfg.count(SystemRole.S2_COORDINATOR) == 5

    def test_systems_for_is_alias_of_count(self):
        """``systems_for`` returns the same value as ``count`` for every role."""
        counts = {r: 1 for r in SystemRole if r is not SystemRole.S1_WORKER}
        counts[SystemRole.S1_WORKER] = 3
        counts[SystemRole.S4_SCANNER] = 7

        cfg = RunConfig(sub_agents_per_role=counts)
        for role in SystemRole:
            assert cfg.systems_for(role) == cfg.count(role)


# ---------------------------------------------------------------------------
# load_config — defaults vs. explicit missing path
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """File-loader behaviour with and without ``vsm.toml`` present."""

    def test_load_config_none_returns_defaults_without_env_or_file(
        self, tmp_path, monkeypatch
    ):
        """``load_config(None)`` with neither env nor ``vsm.toml`` yields defaults."""
        # Move into an empty directory so the default ``vsm.toml`` lookup
        # cannot find a stray file in the project root.
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv(LITELLM_PROVIDER_ENV, raising=False)

        llm_config, run_config = load_config(None)

        # No source → ``provider_from_env`` is None and resolve_model raises.
        assert llm_config.provider_from_env is None
        assert llm_config.provider_from_file is None
        with pytest.raises(ConfigError):
            llm_config.resolve_model()

        # RunConfig falls back to dataclass defaults.
        defaults = RunConfig()
        for role in SystemRole:
            assert run_config.count(role) == defaults.count(role)
        assert run_config.s1_max == defaults.s1_max
        assert run_config.s1_dynamic_max == defaults.s1_dynamic_max
        assert run_config.residency.native_runs_enabled is False

    def test_residency_flag_can_explicitly_enable_native_runs(self, tmp_path, monkeypatch):
        monkeypatch.delenv(LITELLM_PROVIDER_ENV, raising=False)
        toml_path = tmp_path / "vsm.toml"
        toml_path.write_text(
            "[residency]\n"
            "native_runs_enabled = true\n",
            encoding="utf-8",
        )

        _llm_config, run_config = load_config(toml_path)

        assert run_config.residency == ResidencyConfig(native_runs_enabled=True)

    def test_residency_flag_rejects_non_boolean(self, tmp_path, monkeypatch):
        monkeypatch.delenv(LITELLM_PROVIDER_ENV, raising=False)
        toml_path = tmp_path / "vsm.toml"
        toml_path.write_text(
            "[residency]\n"
            'native_runs_enabled = "true"\n',
            encoding="utf-8",
        )

        with pytest.raises(ConfigError, match="native_runs_enabled must be a boolean"):
            load_config(toml_path)

    def test_load_config_none_picks_up_env_when_no_file(
        self, tmp_path, monkeypatch
    ):
        """``LITELLM_PROVIDER`` flows through to the resolved LLMConfig."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(LITELLM_PROVIDER_ENV, "openai/test")

        llm_config, _run_config = load_config(None)

        assert llm_config.provider_from_env == "openai/test"
        assert llm_config.resolve_model() == "openai/test"

    def test_load_config_missing_path_raises(self, tmp_path, monkeypatch):
        """An explicit non-existent path is a hard error (not a fallback)."""
        monkeypatch.delenv(LITELLM_PROVIDER_ENV, raising=False)
        missing = tmp_path / "does-not-exist.toml"
        assert not missing.exists()

        with pytest.raises(ConfigError):
            load_config(missing)

    def test_load_config_explicit_path_reads_file_provider(
        self, tmp_path, monkeypatch
    ):
        """A valid ``vsm.toml`` populates ``provider_from_file``."""
        monkeypatch.delenv(LITELLM_PROVIDER_ENV, raising=False)
        toml_path: Path = tmp_path / "vsm.toml"
        toml_path.write_text(
            '[llm]\nprovider = "anthropic/claude-3-5-sonnet"\n',
            encoding="utf-8",
        )

        llm_config, _run_config = load_config(toml_path)

        assert llm_config.provider_from_env is None
        assert llm_config.provider_from_file == "anthropic/claude-3-5-sonnet"
        assert llm_config.resolve_model() == "anthropic/claude-3-5-sonnet"

    def test_load_config_reads_selfdev_implementation_timeout_margin(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv(LITELLM_PROVIDER_ENV, raising=False)
        toml_path: Path = tmp_path / "vsm.toml"
        toml_path.write_text(
            '[selfdev]\n'
            'enabled = true\n'
            'repository = "."\n'
            'implementation_timeout_margin_seconds = 42.5\n',
            encoding="utf-8",
        )

        _llm_config, run_config = load_config(toml_path)

        assert run_config.selfdev.implementation_timeout_margin_seconds == 42.5
