from pathlib import Path

import pytest

from vsm.config import load_config
from vsm.errors import ConfigurationError


def test_example_benchmark_evidence_cannot_start_runtime():
    example = Path(__file__).parents[1] / "config" / "nanihold.example.toml"
    with pytest.raises(
        ConfigurationError,
        match="example benchmark evidence must be replaced",
    ):
        load_config(example)


def test_local_verification_template_is_explicit_and_loadable(
    tmp_path, monkeypatch
):
    template = (
        Path(__file__).parents[1]
        / "config"
        / "nanihold.local.toml.template"
    )
    rendered = (
        template.read_text("utf-8")
        .replace("@@CLAUDE_VERSION@@", "2.1.215")
        .replace("@@ENVIRONMENT_FINGERPRINT@@", "sha256:test")
        .replace("@@PILOT_HOST_PORT@@", "50001")
        .replace("@@NANIHOLD_WEB_HOST_PORT@@", "50002")
    )
    config_path = tmp_path / "vsm.toml"
    config_path.write_text(rendered, "utf-8")
    monkeypatch.setenv("LETHE_NANIHOLD_TOKEN", "lethe-token")
    monkeypatch.setenv("NANIHOLD_API_BEARER_TOKEN", "api-token")
    monkeypatch.setenv("PILOT_HOST_BEARER_TOKEN", "pilot-token")

    loaded = load_config(config_path)

    assert loaded.config.deployment.mode == "local_verification"
    assert loaded.config.interface_pilot.effort == "low"
    assert loaded.config.pilot.mode == "observe_only"
    assert not loaded.config.pilot.permission_classifier_enabled
    assert loaded.config.kernel.lethe.max_page_size == 100


def test_local_verification_forbids_opus(tmp_path, monkeypatch):
    template = (
        Path(__file__).parents[1]
        / "config"
        / "nanihold.local.toml.template"
    )
    rendered = (
        template.read_text("utf-8")
        .replace("@@CLAUDE_VERSION@@", "2.1.215")
        .replace("@@ENVIRONMENT_FINGERPRINT@@", "sha256:test")
        .replace("@@PILOT_HOST_PORT@@", "50001")
        .replace("@@NANIHOLD_WEB_HOST_PORT@@", "50002")
        .replace("claude-haiku-4-5-20251001", "claude-opus-4-1")
    )
    config_path = tmp_path / "vsm.toml"
    config_path.write_text(rendered, "utf-8")
    monkeypatch.setenv("LETHE_NANIHOLD_TOKEN", "lethe-token")
    monkeypatch.setenv("NANIHOLD_API_BEARER_TOKEN", "api-token")
    monkeypatch.setenv("PILOT_HOST_BEARER_TOKEN", "pilot-token")

    with pytest.raises(ConfigurationError, match="approved cheap exact"):
        load_config(config_path)
