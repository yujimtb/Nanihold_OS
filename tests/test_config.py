from pathlib import Path
import copy
import tomllib

import pytest
from pydantic import ValidationError

from vsm.config import NaniholdConfig, load_config
from vsm.errors import ConfigurationError


def _production_example_data() -> dict[str, object]:
    example = Path(__file__).parents[1] / "config" / "nanihold.example.toml"
    data = tomllib.loads(example.read_text("utf-8"))
    for registration in data["routing"]["candidates"]:  # type: ignore[index]
        for prior in registration["priors"]:  # type: ignore[index]
            prior["version"] = "2026-07"  # type: ignore[index]
            prior["harness"] = "verified-production"  # type: ignore[index]
    return data


def test_production_config_requires_both_coding_escalation_candidates():
    data = _production_example_data()
    config = NaniholdConfig.model_validate(data)
    models = {
        registration.candidate.model_snapshot
        for registration in config.routing.candidates
        if registration.candidate.model_snapshot is not None
    }
    assert {"gpt-5.6-luna", "gpt-5.6-sol"}.issubset(models)

    missing_sol = copy.deepcopy(data)
    missing_sol["routing"]["candidates"] = [  # type: ignore[index]
        registration
        for registration in missing_sol["routing"]["candidates"]  # type: ignore[index]
        if registration["candidate"].get("model_snapshot") != "gpt-5.6-sol"  # type: ignore[index]
    ]
    with pytest.raises(ValidationError, match="requires exactly"):
        NaniholdConfig.model_validate(missing_sol)


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
