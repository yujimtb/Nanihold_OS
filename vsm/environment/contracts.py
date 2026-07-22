"""Portable execution-environment contracts.

This module intentionally contains no environment-instance or host-binding
fields.  An :class:`EnvironmentContract` describes capabilities only; the
contract is therefore safe to use as candidate identity input and to move
between hosts.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from enum import StrEnum
from typing import Any, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnvironmentModel(BaseModel):
    """Strict, immutable model base for the EEP contract layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SandboxMode(StrEnum):
    """Sandbox modes understood by the contract layer."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


_LOGICAL_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_CLI_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")
_KNOWN_SHELLS = frozenset({"powershell", "posix", "cmd"})


def _normalise_collection(value: Any) -> Any:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return value
    return tuple(sorted(str(item).strip() for item in value))


def _validate_endpoints(value: tuple[str, ...], owner: str) -> tuple[str, ...]:
    result: list[str] = []
    for endpoint in value:
        if not endpoint:
            raise ValueError(f"{owner} endpoints must not be blank")
        if (
            endpoint.startswith(("/", "~"))
            or re.match(r"^[A-Za-z]:[\\/]", endpoint)
            or any(token in endpoint for token in ("\\", "CODEX_HOME", "$HOME"))
        ):
            raise ValueError(f"{owner} endpoint contains host-specific data")
        parsed = urlsplit(endpoint if "://" in endpoint else f"//{endpoint}")
        host = parsed.hostname
        if host is None:
            raise ValueError(f"{owner} endpoint is invalid: {endpoint!r}")
        lowered_host = host.lower()
        if lowered_host in {
            "localhost",
            "host.docker.internal",
            "ip6-localhost",
        }:
            raise ValueError(f"{owner} endpoints must not name a local host")
        try:
            ipaddress.ip_address(lowered_host)
        except ValueError:
            pass
        else:
            raise ValueError(f"{owner} endpoints must use portable DNS names")
        result.append(endpoint.lower())
    if len(result) != len(set(result)):
        raise ValueError(f"{owner} required_endpoints must be unique")
    return tuple(sorted(result))


class AdapterRequirement(EnvironmentModel):
    """Requirements contributed by one execution CLI adapter."""

    required_endpoints: tuple[str, ...] = Field(min_length=1)
    minimum_cli_version: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalise_collections_and_text(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        normalised = dict(value)
        if "required_endpoints" in normalised:
            normalised["required_endpoints"] = _normalise_collection(
                normalised["required_endpoints"]
            )
        item = normalised.get("minimum_cli_version")
        if isinstance(item, str):
            normalised["minimum_cli_version"] = item.strip()
        return normalised

    @field_validator("required_endpoints")
    @classmethod
    def endpoints_are_portable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_endpoints(value, "AdapterRequirement")

    @field_validator("minimum_cli_version")
    @classmethod
    def cli_version_is_a_version(cls, value: str | None) -> str | None:
        if value is not None and not _CLI_VERSION.fullmatch(value):
            raise ValueError(
                "AdapterRequirement minimum_cli_version must be a version, not a path"
            )
        return value


class EnvironmentContract(EnvironmentModel):
    """Machine-independent capabilities required from an execution environment.

    The field set is deliberately closed.  In particular, execution-location
    names, physical paths, CLI executable paths, and host environment values
    are not part of this model and are rejected as extra fields.
    """

    supported_shells: tuple[str, ...] = Field(min_length=1)
    workspace_writable: bool
    minimum_memory_mb: int = Field(gt=0)
    supported_sandboxes: tuple[SandboxMode, ...] = Field(min_length=1)
    required_sandbox: SandboxMode
    path_mapping_names: tuple[str, ...] = Field(min_length=1)
    adapters: Mapping[str, AdapterRequirement]

    @model_validator(mode="before")
    @classmethod
    def normalise_collections_and_text(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        normalised = dict(value)
        for field_name in (
            "supported_shells",
            "supported_sandboxes",
            "path_mapping_names",
        ):
            if field_name in normalised:
                normalised[field_name] = _normalise_collection(normalised[field_name])
        for field_name in ("required_sandbox",):
            item = normalised.get(field_name)
            if isinstance(item, str):
                normalised[field_name] = item.strip().lower()
        return normalised

    @field_validator("supported_shells")
    @classmethod
    def shells_are_known_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("EnvironmentContract supported_shells must be unique")
        unknown = sorted(set(value) - _KNOWN_SHELLS)
        if unknown:
            raise ValueError(
                "EnvironmentContract supported_shells contains unknown shell(s): "
                + ", ".join(unknown)
            )
        return tuple(sorted(value))

    @field_validator("supported_sandboxes")
    @classmethod
    def sandboxes_are_unique(cls, value: tuple[SandboxMode, ...]) -> tuple[SandboxMode, ...]:
        if len(value) != len(set(value)):
            raise ValueError("EnvironmentContract supported_sandboxes must be unique")
        return tuple(sorted(value, key=lambda item: item.value))

    @field_validator("path_mapping_names")
    @classmethod
    def path_names_are_logical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("EnvironmentContract path_mapping_names must be unique")
        if any(not _LOGICAL_NAME.fullmatch(item) for item in value):
            raise ValueError(
                "EnvironmentContract path_mapping_names must contain logical names only"
            )
        return tuple(sorted(value))

    @field_validator("adapters")
    @classmethod
    def adapters_are_named_and_non_empty(
        cls, value: Mapping[str, AdapterRequirement]
    ) -> dict[str, AdapterRequirement]:
        if not value:
            raise ValueError("EnvironmentContract adapters must not be empty")
        normalized: dict[str, AdapterRequirement] = {}
        for name, requirement in value.items():
            if not isinstance(name, str) or not _LOGICAL_NAME.fullmatch(name.strip()):
                raise ValueError(
                    "EnvironmentContract adapter names must be logical names"
                )
            normalized_name = name.strip()
            if normalized_name in normalized:
                raise ValueError("EnvironmentContract adapter names must be unique")
            normalized[normalized_name] = requirement
        return dict(sorted(normalized.items()))

    @model_validator(mode="after")
    def required_sandbox_is_supported(self) -> "EnvironmentContract":
        if self.required_sandbox not in self.supported_sandboxes:
            raise ValueError(
                "EnvironmentContract required_sandbox must be supported_sandboxes"
            )
        return self

    @property
    def required_endpoints(self) -> tuple[str, ...]:
        """Return the union of all adapter endpoint requirements."""

        return tuple(
            sorted(
                {
                    endpoint
                    for requirement in self.adapters.values()
                    for endpoint in requirement.required_endpoints
                }
            )
        )

    def canonical_document(self) -> dict[str, object]:
        """Return the normalized, machine-independent fingerprint document."""

        document = self.model_dump(mode="json")
        document["adapters"] = {
            name: self.adapters[name].model_dump(mode="json")
            for name in sorted(self.adapters)
        }
        return document


ENVIRONMENT_FINGERPRINT_PREFIX = "environment-contract-sha256:"


def environment_fingerprint(contract: EnvironmentContract) -> str:
    """Calculate the environment fingerprint from the contract alone.

    The concrete type requirement is intentional: callers cannot accidentally
    pass an environment instance or a mixed dictionary containing host data.
    Collection order and JSON key order are normalized before hashing.
    """

    if type(contract) is not EnvironmentContract:
        raise TypeError("environment_fingerprint requires an EnvironmentContract")
    canonical = json.dumps(
        contract.canonical_document(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ENVIRONMENT_FINGERPRINT_PREFIX + hashlib.sha256(canonical).hexdigest()
