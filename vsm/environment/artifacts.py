"""Versioned EnvironmentContract artifact storage.

The store protocol is the Track B connection point.  A LETHE adapter supplies
versioned artifact transport; this module provides the deterministic artifact
format and a local file implementation for development and tests.
"""

from __future__ import annotations

import json
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import Field, field_validator, model_validator

from vsm.errors import InvariantViolation

from .contracts import EnvironmentContract, EnvironmentModel, environment_fingerprint


class EnvironmentContractArtifactType(StrEnum):
    """Artifact type value reserved for environment contracts."""

    ENVIRONMENT_CONTRACT = "environment-contract"


_ARTIFACT_KEY = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")


class EnvironmentContractArtifact(EnvironmentModel):
    """Self-describing, immutable, versioned contract artifact."""

    artifact_type: str = EnvironmentContractArtifactType.ENVIRONMENT_CONTRACT.value
    artifact_key: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    schema_version: str = "1.0.0"
    fingerprint: str = Field(
        pattern=r"^environment-contract-sha256:[0-9a-f]{64}$"
    )
    contract: EnvironmentContract

    @field_validator("artifact_key")
    @classmethod
    def artifact_key_is_path_safe(cls, value: str) -> str:
        value = value.strip()
        if not _ARTIFACT_KEY.fullmatch(value):
            raise ValueError("EnvironmentContract artifact_key must be a path-safe logical key")
        return value

    @model_validator(mode="after")
    def metadata_matches_contract(self) -> "EnvironmentContractArtifact":
        if self.artifact_type != EnvironmentContractArtifactType.ENVIRONMENT_CONTRACT.value:
            raise ValueError("artifact_type must be environment-contract")
        if self.schema_version != "1.0.0":
            raise ValueError("unsupported EnvironmentContract artifact schema_version")
        if self.fingerprint != environment_fingerprint(self.contract):
            raise ValueError("EnvironmentContract artifact fingerprint mismatch")
        return self


class VersionedArtifactTransport(Protocol):
    """Track B transport contract for versioned LETHE artifacts."""

    def put_versioned_artifact(
        self,
        *,
        artifact_type: str,
        artifact_key: str,
        version: int,
        payload: bytes,
    ) -> None: ...

    def get_versioned_artifact(
        self,
        *,
        artifact_type: str,
        artifact_key: str,
        version: int,
    ) -> bytes: ...


class EnvironmentContractArtifactStore(Protocol):
    """Contract-layer repository implemented by local and LETHE stores."""

    def save(
        self,
        contract: EnvironmentContract,
        *,
        artifact_key: str,
        version: int,
    ) -> EnvironmentContractArtifact: ...

    def get(self, *, artifact_key: str, version: int) -> EnvironmentContractArtifact: ...


def _artifact_for(
    contract: EnvironmentContract, *, artifact_key: str, version: int
) -> EnvironmentContractArtifact:
    return EnvironmentContractArtifact(
        artifact_key=artifact_key,
        version=version,
        fingerprint=environment_fingerprint(contract),
        contract=contract,
    )


def serialize_environment_contract_artifact(
    contract: EnvironmentContract, *, artifact_key: str, version: int
) -> bytes:
    """Serialize a contract artifact as canonical UTF-8 JSON."""

    artifact = _artifact_for(contract, artifact_key=artifact_key, version=version)
    return json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def deserialize_environment_contract_artifact(
    payload: bytes,
    *,
    expected_artifact_key: str | None = None,
    expected_version: int | None = None,
) -> EnvironmentContractArtifact:
    """Deserialize and validate a versioned contract artifact."""

    try:
        artifact = EnvironmentContractArtifact.model_validate_json(payload)
    except Exception as exc:
        raise InvariantViolation("invalid EnvironmentContract artifact") from exc
    if (
        expected_artifact_key is not None
        and artifact.artifact_key != expected_artifact_key
    ):
        raise InvariantViolation("EnvironmentContract artifact key mismatch")
    if expected_version is not None and artifact.version != expected_version:
        raise InvariantViolation("EnvironmentContract artifact version mismatch")
    return artifact


class LocalEnvironmentContractStore:
    """Filesystem-backed versioned store for local development."""

    def __init__(self, root: Path) -> None:
        if not root:
            raise InvariantViolation("local EnvironmentContract store root is required")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _path_for(root: Path, *, artifact_key: str, version: int) -> Path:
        if not _ARTIFACT_KEY.fullmatch(artifact_key):
            raise InvariantViolation("EnvironmentContract artifact key is not path-safe")
        if version < 1:
            raise InvariantViolation("EnvironmentContract artifact version must be positive")
        return (
            root
            / EnvironmentContractArtifactType.ENVIRONMENT_CONTRACT.value
            / artifact_key
            / f"v{version}.json"
        )

    def save(
        self,
        contract: EnvironmentContract,
        *,
        artifact_key: str,
        version: int,
    ) -> EnvironmentContractArtifact:
        artifact = _artifact_for(contract, artifact_key=artifact_key, version=version)
        path = self._path_for(self.root, artifact_key=artifact_key, version=version)
        payload = serialize_environment_contract_artifact(
            contract, artifact_key=artifact_key, version=version
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = deserialize_environment_contract_artifact(
                path.read_bytes(),
                expected_artifact_key=artifact_key,
                expected_version=version,
            )
            if existing != artifact:
                raise InvariantViolation(
                    "EnvironmentContract artifact version is immutable"
                )
            return existing
        temporary = path.with_suffix(".json.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
        return artifact

    def get(self, *, artifact_key: str, version: int) -> EnvironmentContractArtifact:
        path = self._path_for(self.root, artifact_key=artifact_key, version=version)
        if not path.is_file():
            raise InvariantViolation(
                f"EnvironmentContract artifact not found: {artifact_key}@{version}"
            )
        return deserialize_environment_contract_artifact(
            path.read_bytes(),
            expected_artifact_key=artifact_key,
            expected_version=version,
        )

    def get_contract(self, *, artifact_key: str, version: int) -> EnvironmentContract:
        """Retrieve only the validated contract payload."""

        return self.get(artifact_key=artifact_key, version=version).contract


class LetheEnvironmentContractStore:
    """LETHE-facing store using a Track B versioned-artifact transport."""

    def __init__(self, transport: VersionedArtifactTransport) -> None:
        self._transport = transport

    def save(
        self,
        contract: EnvironmentContract,
        *,
        artifact_key: str,
        version: int,
    ) -> EnvironmentContractArtifact:
        artifact = _artifact_for(contract, artifact_key=artifact_key, version=version)
        self._transport.put_versioned_artifact(
            artifact_type=EnvironmentContractArtifactType.ENVIRONMENT_CONTRACT.value,
            artifact_key=artifact_key,
            version=version,
            payload=serialize_environment_contract_artifact(
                contract, artifact_key=artifact_key, version=version
            ),
        )
        return artifact

    def get(self, *, artifact_key: str, version: int) -> EnvironmentContractArtifact:
        payload = self._transport.get_versioned_artifact(
            artifact_type=EnvironmentContractArtifactType.ENVIRONMENT_CONTRACT.value,
            artifact_key=artifact_key,
            version=version,
        )
        return deserialize_environment_contract_artifact(
            payload,
            expected_artifact_key=artifact_key,
            expected_version=version,
        )
