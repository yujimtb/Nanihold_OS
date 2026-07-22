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


class ShellKind(StrEnum):
    """Shell capability requested by a portable environment contract."""

    POSIX = "posix"
    POWERSHELL = "powershell"
    CMD = "cmd"


class SandboxMode(StrEnum):
    """Sandbox modes understood by the contract layer."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"


_LOGICAL_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_CLI_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?$")


def _normalise_collection(value: Any) -> Any:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return value
    return tuple(sorted(str(item).strip() for item in value))


class EnvironmentContract(EnvironmentModel):
    """Machine-independent capabilities required from an execution environment.

    The field set is deliberately closed.  In particular, execution-location
    names, physical paths, CLI executable paths, and host environment values
    are not part of this model and are rejected as extra fields.
    """

    required_shell: ShellKind
    required_endpoints: tuple[str, ...] = Field(min_length=1)
    workspace_writable: bool
    minimum_memory_mb: int = Field(gt=0)
    supported_sandboxes: tuple[SandboxMode, ...] = Field(min_length=1)
    required_sandbox: SandboxMode
    path_mapping_names: tuple[str, ...] = Field(min_length=1)
    minimum_cli_version: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalise_collections_and_text(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        normalised = dict(value)
        for field_name in (
            "required_endpoints",
            "supported_sandboxes",
            "path_mapping_names",
        ):
            if field_name in normalised:
                normalised[field_name] = _normalise_collection(normalised[field_name])
        for field_name in ("minimum_cli_version",):
            item = normalised.get(field_name)
            if isinstance(item, str):
                normalised[field_name] = item.strip()
        for field_name in ("required_shell", "required_sandbox"):
            item = normalised.get(field_name)
            if isinstance(item, str):
                normalised[field_name] = item.strip().lower()
        return normalised

    @field_validator("required_endpoints")
    @classmethod
    def endpoints_are_portable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("EnvironmentContract required_endpoints must be unique")
        result: list[str] = []
        for endpoint in value:
            if not endpoint:
                raise ValueError("EnvironmentContract endpoints must not be blank")
            if (
                endpoint.startswith(("/", "~"))
                or re.match(r"^[A-Za-z]:[\\/]", endpoint)
                or any(token in endpoint for token in ("\\", "CODEX_HOME", "$HOME"))
            ):
                raise ValueError("EnvironmentContract endpoint contains host-specific data")
            parsed = urlsplit(endpoint if "://" in endpoint else f"//{endpoint}")
            host = parsed.hostname
            if host is None:
                raise ValueError(f"EnvironmentContract endpoint is invalid: {endpoint!r}")
            lowered_host = host.lower()
            if lowered_host in {
                "localhost",
                "host.docker.internal",
                "ip6-localhost",
            }:
                raise ValueError("EnvironmentContract endpoints must not name a local host")
            try:
                ipaddress.ip_address(lowered_host)
            except ValueError:
                pass
            else:
                raise ValueError("EnvironmentContract endpoints must use portable DNS names")
            result.append(endpoint.lower())
        return tuple(sorted(result))

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

    @field_validator("minimum_cli_version")
    @classmethod
    def cli_version_is_a_version(cls, value: str | None) -> str | None:
        if value is not None and not _CLI_VERSION.fullmatch(value):
            raise ValueError(
                "EnvironmentContract minimum_cli_version must be a version, not a path"
            )
        return value

    @model_validator(mode="after")
    def required_sandbox_is_supported(self) -> "EnvironmentContract":
        if self.required_sandbox not in self.supported_sandboxes:
            raise ValueError(
                "EnvironmentContract required_sandbox must be supported_sandboxes"
            )
        return self

    def canonical_document(self) -> dict[str, object]:
        """Return the normalized, machine-independent fingerprint document."""

        return self.model_dump(mode="json")


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
