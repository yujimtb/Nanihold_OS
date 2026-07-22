from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from vsm.config import load_config
from vsm.environment import (
    EnvironmentContract,
    LocalEnvironmentContractStore,
    OwnerApprovalRequest,
    OwnerApprovalTarget,
    ProcurementPolicyBoundary,
    SandboxMode,
    deserialize_environment_contract_artifact,
    environment_fingerprint,
    serialize_environment_contract_artifact,
)
from vsm.errors import ConfigurationError


def _contract(**updates: object) -> EnvironmentContract:
    data: dict[str, object] = {
        "supported_shells": ["posix"],
        "required_endpoints": ["api.openai.com", "api.anthropic.com"],
        "workspace_writable": True,
        "minimum_memory_mb": 4096,
        "supported_sandboxes": ["read-only", "workspace-write"],
        "required_sandbox": "workspace-write",
        "path_mapping_names": ["workspace-root"],
        "minimum_cli_version": "0.144.5",
    }
    data.update(updates)
    return EnvironmentContract.model_validate(data)


def test_environment_contract_is_capability_only_and_rejects_host_fields():
    contract = _contract()

    assert set(EnvironmentContract.model_fields) == {
        "supported_shells",
        "required_endpoints",
        "workspace_writable",
        "minimum_memory_mb",
        "supported_sandboxes",
        "required_sandbox",
        "path_mapping_names",
        "minimum_cli_version",
    }
    assert "workspace-root" in contract.path_mapping_names
    assert not any(
        "path" in field and field != "path_mapping_names"
        for field in EnvironmentContract.model_fields
    )
    assert contract.supported_shells == ("posix",)
    assert contract.required_sandbox is SandboxMode.WORKSPACE_WRITE

    with pytest.raises(ValidationError):
        EnvironmentContract.model_validate(
            {
                **contract.model_dump(),
                "execution_location": "wsl-ubuntu",
            }
        )
    with pytest.raises(ValidationError):
        EnvironmentContract.model_validate(
            {
                **contract.model_dump(),
                "machine_path": "D:\\workspace",
            }
        )


def test_environment_fingerprint_is_normalized_and_contract_only():
    first = _contract(
        required_endpoints=[" api.openai.com ", "api.anthropic.com"],
        supported_sandboxes=["workspace-write", "read-only"],
        path_mapping_names=["workspace-root"],
    )
    second = _contract(
        required_endpoints=["api.anthropic.com", "api.openai.com"],
        supported_sandboxes=["read-only", "workspace-write"],
        path_mapping_names=["workspace-root"],
    )
    changed = _contract(minimum_memory_mb=8192)
    powershell_first = _contract(supported_shells=["powershell", "posix"])
    posix_first = _contract(supported_shells=["posix", "powershell"])

    assert environment_fingerprint(first) == environment_fingerprint(second)
    assert environment_fingerprint(powershell_first) == environment_fingerprint(posix_first)
    assert environment_fingerprint(first).startswith("environment-contract-sha256:")
    assert environment_fingerprint(first) != environment_fingerprint(changed)
    assert list(inspect.signature(environment_fingerprint).parameters) == ["contract"]
    with pytest.raises(TypeError):
        environment_fingerprint({"contract": first})  # type: ignore[arg-type]


def test_supported_shells_are_non_empty_unique_and_known():
    with pytest.raises(ValidationError):
        _contract(supported_shells=[])
    with pytest.raises(ValidationError, match="supported_shells must be unique"):
        _contract(supported_shells=["posix", "posix"])
    with pytest.raises(ValidationError, match="unknown shell"):
        _contract(supported_shells=["posix", "fish"])


def test_versioned_artifact_round_trip_preserves_fingerprint(tmp_path: Path):
    contract = _contract()
    store = LocalEnvironmentContractStore(tmp_path / "contracts")

    saved = store.save(contract, artifact_key="coding", version=3)
    loaded = store.get(artifact_key="coding", version=3)
    payload = serialize_environment_contract_artifact(
        contract, artifact_key="coding", version=3
    )
    decoded = deserialize_environment_contract_artifact(
        payload, expected_artifact_key="coding", expected_version=3
    )

    assert saved.version == 3
    assert loaded.contract == contract
    assert decoded.contract == contract
    assert loaded.fingerprint == environment_fingerprint(contract)
    assert decoded.fingerprint == loaded.fingerprint


def test_owner_approval_target_contains_only_contract_and_boundary():
    boundary = ProcurementPolicyBoundary(
        allowed_resources=("cpu", "memory"),
        allowed_networks=("api.openai.com",),
        budget_currency="USD",
        maximum_budget="25.00",
    )
    target = OwnerApprovalTarget(
        environment_contract=_contract(),
        procurement_policy_boundary=boundary,
    )
    request = OwnerApprovalRequest(target=target)

    assert {item.value for item in request.target.target_kinds} == {
        "environment_contract",
        "procurement_policy_boundary",
    }
    with pytest.raises(ValidationError):
        OwnerApprovalRequest.model_validate(
            {"target": target.model_dump(), "operation": "discover"}
        )


def test_declared_environment_fingerprint_mismatch_is_configuration_error(tmp_path: Path):
    example = Path(__file__).parents[1] / "config" / "nanihold.example.toml"
    config_path = tmp_path / "vsm.toml"
    fingerprint = "environment-contract-sha256:f0fa8e57493b08815e5ab2389dfbf6193030a08d5dc3a72eba160f6138a4496d"
    config_path.write_text(
        example.read_text("utf-8").replace(
            fingerprint, "environment-contract-sha256:" + "0" * 64, 1
        ),
        "utf-8",
    )

    with pytest.raises(ConfigurationError, match="environment_fingerprint"):
        load_config(config_path)
