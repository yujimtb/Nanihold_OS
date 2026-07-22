"""Dispatch-time execution-environment preflight and its durable cache.

This module deliberately knows nothing about the Operational Ledger.  It returns
structured evidence and exposes hooks so the PilotHost boundary can hand that
evidence to the ledger integration without coupling this module to it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from vsm.environment import EnvironmentContract, environment_fingerprint


class PreflightError(RuntimeError):
    """Base error for a closed-gate preflight failure."""


class VersionReadError(PreflightError):
    """The configured CLI version file could not be read deterministically."""


class PreflightContractError(PreflightError):
    """The measured environment does not satisfy its declared contract."""


class PreflightCacheError(PreflightError):
    """The durable preflight cache could not be read or written safely."""


@dataclass(frozen=True)
class CliVersion:
    version: str
    version_file: str
    version_file_mtime_ns: int


class CliVersionReader:
    """Read a CLI version from a package version file without starting a process.

    ``package.json`` files are parsed using ``version_field``.  Other files are
    treated as a single plain-text version.  The file is stat'ed before and
    after reading so an update racing with a dispatch fails closed instead of
    producing an untraceable version value.
    """

    def __init__(self, version_file: Path, *, version_field: str = "version") -> None:
        if not isinstance(version_file, Path):
            raise TypeError("version_file must be a pathlib.Path")
        if not version_field.strip():
            raise ValueError("version_field must not be blank")
        self.version_file = version_file
        self.version_field = version_field

    def read(self) -> CliVersion:
        try:
            before = self.version_file.stat()
            raw = self.version_file.read_text(encoding="utf-8")
            after = self.version_file.stat()
        except (OSError, UnicodeError) as exc:
            raise VersionReadError(
                f"CLI version file could not be read: {self.version_file}"
            ) from exc
        if before.st_mtime_ns != after.st_mtime_ns:
            raise VersionReadError(
                f"CLI version file changed while it was being read: {self.version_file}"
            )

        if self.version_file.name == "package.json" or self.version_file.suffix == ".json":
            try:
                document = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise VersionReadError(
                    f"CLI version file is not valid JSON: {self.version_file}"
                ) from exc
            if not isinstance(document, dict):
                raise VersionReadError("CLI package version document must be an object")
            version = document.get(self.version_field)
        else:
            version = raw.strip()
        if not isinstance(version, str) or not version.strip():
            raise VersionReadError(
                f"CLI version file does not contain a non-blank version: {self.version_file}"
            )
        return CliVersion(
            version=version.strip(),
            version_file=str(self.version_file),
            version_file_mtime_ns=before.st_mtime_ns,
        )


@dataclass(frozen=True)
class VerificationTuple:
    cli_version: str
    sandbox_mode: str
    environment_fingerprint: str


@dataclass(frozen=True)
class PreflightObservation:
    """Measured values returned by one injected Codex preflight trial."""

    sandbox_policy: str
    capabilities: Mapping[str, object]
    rollout_ref: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "PreflightObservation":
        sandbox_policy = value.get("sandbox_policy")
        if sandbox_policy is None:
            rollout = value.get("rollout")
            if isinstance(rollout, Mapping):
                sandbox_policy = rollout.get("sandbox_policy")
        sandbox_policy = _nonblank(sandbox_policy, "measured sandbox_policy")
        capabilities = value.get("capabilities", {})
        if not isinstance(capabilities, Mapping):
            raise PreflightContractError("preflight capabilities must be an object")
        rollout_ref = value.get("rollout_ref")
        if rollout_ref is not None:
            rollout_ref = _nonblank(rollout_ref, "rollout_ref")
        # Flat capability fields are accepted by the injected runner contract as
        # well as the nested form, while the normalized evidence always nests them.
        flat_capabilities = {
            key: item
            for key, item in value.items()
            if key in {
                "workspace_writable",
                "endpoint_reachable",
                "memory_bytes",
                "shell",
                "path_mappings",
            }
        }
        merged = dict(capabilities)
        merged.update(flat_capabilities)
        return cls(
            sandbox_policy=sandbox_policy,
            capabilities=merged,
            rollout_ref=rollout_ref,
        )


@dataclass(frozen=True)
class PreflightEvidence:
    verification_tuple: VerificationTuple
    measured_sandbox_policy: str
    measured_capabilities: Mapping[str, object]
    instance_fingerprint: str
    checked_at: str
    rollout_ref: str | None
    version_file: str
    version_file_mtime_ns: int

    def to_dict(self) -> dict[str, object]:
        return {
            "verification_tuple": asdict(self.verification_tuple),
            "measured_sandbox_policy": self.measured_sandbox_policy,
            "measured_capabilities": dict(self.measured_capabilities),
            "instance_fingerprint": self.instance_fingerprint,
            "checked_at": self.checked_at,
            "rollout_ref": self.rollout_ref,
            "version_file": self.version_file,
            "version_file_mtime_ns": self.version_file_mtime_ns,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "PreflightEvidence":
        raw_tuple = value.get("verification_tuple")
        if not isinstance(raw_tuple, Mapping):
            raise PreflightCacheError("preflight cache evidence has no verification tuple")
        verification_tuple = VerificationTuple(
            cli_version=_nonblank(raw_tuple.get("cli_version"), "cached cli_version"),
            sandbox_mode=_nonblank(raw_tuple.get("sandbox_mode"), "cached sandbox_mode"),
            environment_fingerprint=_nonblank(
                raw_tuple.get("environment_fingerprint"),
                "cached environment_fingerprint",
            ),
        )
        capabilities = value.get("measured_capabilities", {})
        if not isinstance(capabilities, Mapping):
            raise PreflightCacheError("cached measured_capabilities must be an object")
        mtime = value.get("version_file_mtime_ns")
        if not isinstance(mtime, int) or isinstance(mtime, bool) or mtime < 0:
            raise PreflightCacheError("cached version_file_mtime_ns is invalid")
        return cls(
            verification_tuple=verification_tuple,
            measured_sandbox_policy=_nonblank(
                value.get("measured_sandbox_policy"), "cached measured_sandbox_policy"
            ),
            measured_capabilities=dict(capabilities),
            instance_fingerprint=_nonblank(
                value.get("instance_fingerprint"), "cached instance_fingerprint"
            ),
            checked_at=_nonblank(value.get("checked_at"), "cached checked_at"),
            rollout_ref=(
                None
                if value.get("rollout_ref") is None
                else _nonblank(value.get("rollout_ref"), "cached rollout_ref")
            ),
            version_file=_nonblank(value.get("version_file"), "cached version_file"),
            version_file_mtime_ns=mtime,
        )


@dataclass(frozen=True)
class PreflightResult:
    evidence: PreflightEvidence
    cache_hit: bool


@dataclass(frozen=True)
class DeclarationUpdateEvent:
    field: str
    occurred_at: str
    from_value: str | None
    to_value: str
    source: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PreflightRunner(Protocol):
    def __call__(self, verification_tuple: VerificationTuple) -> PreflightObservation | Mapping[str, object]:
        ...


def candidate_metadata_updater(
    declaration: MutableMapping[str, object],
    *,
    event_hook: Callable[[DeclarationUpdateEvent], object] | None = None,
    persist_hook: Callable[[], object] | None = None,
) -> Callable[[PreflightEvidence], DeclarationUpdateEvent]:
    """Return the deterministic FAV-06 candidate-version updater.

    The caller owns the declaration document and may provide ``persist_hook`` to
    atomically persist the surrounding document.  No model identity or key is
    recalculated here.
    """

    if not isinstance(declaration, MutableMapping):
        raise TypeError("candidate declaration must be mutable")

    def update(evidence: PreflightEvidence) -> DeclarationUpdateEvent:
        old_value = declaration.get("adapter_version")
        if old_value is not None and not isinstance(old_value, str):
            raise PreflightContractError("candidate adapter_version must be a string")
        declaration["adapter_version"] = evidence.verification_tuple.cli_version
        event = DeclarationUpdateEvent(
            field="candidate.adapter_version",
            occurred_at=evidence.checked_at,
            from_value=old_value,
            to_value=evidence.verification_tuple.cli_version,
            source="dispatch_preflight",
            reason="cli_version_file_changed",
        )
        changed = old_value != evidence.verification_tuple.cli_version
        if changed and persist_hook is not None:
            try:
                persist_hook()
            except Exception as exc:
                raise PreflightContractError(
                    "candidate declaration could not be persisted"
                ) from exc
        if changed and event_hook is not None:
            event_hook(event)
        return event

    return update


class PreflightGate:
    """Dispatch-time version gate, preflight runner, and durable cache."""

    CACHE_SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        contract: EnvironmentContract,
        instance_fingerprint: str,
        version_reader: CliVersionReader,
        cache_path: Path,
        preflight_runner: PreflightRunner,
        declaration_updater: Callable[[PreflightEvidence], object] | None = None,
        candidate_declaration: MutableMapping[str, object] | None = None,
        declaration_event_hook: Callable[[DeclarationUpdateEvent], object] | None = None,
        declaration_persist_hook: Callable[[], object] | None = None,
        evidence_hook: Callable[[PreflightEvidence], object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(contract) is not EnvironmentContract:
            raise TypeError("contract must be an EnvironmentContract")
        self.contract = contract
        self.instance_fingerprint = _nonblank(instance_fingerprint, "instance_fingerprint")
        self.version_reader = version_reader
        self.cache_path = cache_path
        self.preflight_runner = preflight_runner
        self._lock = threading.RLock()
        if declaration_updater is not None and candidate_declaration is not None:
            raise ValueError("provide declaration_updater or candidate_declaration, not both")
        self.declaration_updater = declaration_updater
        if candidate_declaration is not None:
            self.declaration_updater = candidate_metadata_updater(
                candidate_declaration,
                event_hook=declaration_event_hook,
                persist_hook=declaration_persist_hook,
            )
        self.evidence_hook = evidence_hook
        self.clock = clock or (lambda: datetime.now(UTC))

    def dispatch_preflight(self) -> PreflightResult:
        """Read the version and gate the next dispatch immediately before launch."""

        with self._lock:
            return self._dispatch_preflight()

    def _dispatch_preflight(self) -> PreflightResult:

        cli_version = self.version_reader.read()
        verification_tuple = VerificationTuple(
            cli_version=cli_version.version,
            sandbox_mode=self.contract.required_sandbox.value,
            environment_fingerprint=environment_fingerprint(self.contract),
        )
        cached = self._find_cached(verification_tuple, cli_version.version_file_mtime_ns)
        if cached is not None:
            return PreflightResult(evidence=cached, cache_hit=True)

        try:
            raw_observation = self.preflight_runner(verification_tuple)
        except PreflightError:
            raise
        except Exception as exc:
            raise PreflightError("preflight trial could not be completed") from exc
        try:
            if isinstance(raw_observation, PreflightObservation):
                observation = raw_observation
            elif isinstance(raw_observation, Mapping):
                observation = PreflightObservation.from_mapping(raw_observation)
            else:
                raise PreflightContractError("preflight trial returned a non-object")
        except PreflightError:
            raise
        except Exception as exc:
            raise PreflightError("preflight trial returned invalid evidence") from exc
        self._validate_observation(verification_tuple, observation)
        checked_at = self.clock()
        if checked_at.tzinfo is None:
            raise PreflightError("preflight clock must return a timezone-aware datetime")
        evidence = PreflightEvidence(
            verification_tuple=verification_tuple,
            measured_sandbox_policy=observation.sandbox_policy,
            measured_capabilities=dict(observation.capabilities),
            instance_fingerprint=self.instance_fingerprint,
            checked_at=checked_at.astimezone(UTC).isoformat(),
            rollout_ref=observation.rollout_ref,
            version_file=cli_version.version_file,
            version_file_mtime_ns=cli_version.version_file_mtime_ns,
        )
        if self.declaration_updater is not None:
            try:
                self.declaration_updater(evidence)
            except PreflightError:
                raise
            except Exception as exc:
                raise PreflightError("candidate declaration update failed") from exc
        if self.evidence_hook is not None:
            try:
                self.evidence_hook(evidence)
            except Exception as exc:
                raise PreflightError("preflight evidence hook failed") from exc
        self._store(evidence)
        return PreflightResult(evidence=evidence, cache_hit=False)

    def _validate_observation(
        self,
        verification_tuple: VerificationTuple,
        observation: PreflightObservation,
    ) -> None:
        supported_sandboxes = {
            sandbox.value for sandbox in self.contract.supported_sandboxes
        }
        if observation.sandbox_policy not in supported_sandboxes:
            raise PreflightContractError(
                "measured sandbox_policy is not supported by the environment contract"
            )
        if observation.sandbox_policy != verification_tuple.sandbox_mode:
            raise PreflightContractError(
                "measured sandbox_policy differs from the required sandbox mode"
            )
        capabilities = observation.capabilities
        if self.contract.workspace_writable and capabilities.get("workspace_writable") is not True:
            raise PreflightContractError("workspace write capability was not measured")
        endpoint_values = capabilities.get("endpoint_reachable")
        if isinstance(endpoint_values, Mapping):
            missing_endpoints = [
                endpoint
                for endpoint in self.contract.required_endpoints
                if endpoint_values.get(endpoint) is not True
            ]
        elif isinstance(endpoint_values, Sequence) and not isinstance(
            endpoint_values, (str, bytes)
        ):
            reachable = set(endpoint_values)
            missing_endpoints = [
                endpoint
                for endpoint in self.contract.required_endpoints
                if endpoint not in reachable
            ]
        else:
            missing_endpoints = list(self.contract.required_endpoints)
        if missing_endpoints:
            raise PreflightContractError(
                "required endpoints were not measured as reachable: "
                + ", ".join(missing_endpoints)
            )
        memory = capabilities.get("memory_bytes")
        minimum_memory_bytes = self.contract.minimum_memory_mb * 1024 * 1024
        if (
            not isinstance(memory, int)
            or isinstance(memory, bool)
            or memory < minimum_memory_bytes
        ):
            raise PreflightContractError("minimum memory capability was not met")
        if capabilities.get("shell") != self.contract.required_shell.value:
            raise PreflightContractError("required shell capability was not measured")
        path_mappings = capabilities.get("path_mappings")
        if isinstance(path_mappings, Mapping):
            measured_path_names = set(path_mappings)
        elif isinstance(path_mappings, Sequence) and not isinstance(
            path_mappings, (str, bytes)
        ):
            measured_path_names = set(path_mappings)
        else:
            measured_path_names = set()
        missing_path_names = [
            name
            for name in self.contract.path_mapping_names
            if name not in measured_path_names
        ]
        if missing_path_names:
            raise PreflightContractError(
                "required path mappings were not measured: "
                + ", ".join(missing_path_names)
            )
        if self.contract.minimum_cli_version is not None and _compare_versions(
            verification_tuple.cli_version, self.contract.minimum_cli_version
        ) < 0:
            raise PreflightContractError("CLI version is below the contract minimum")

    def _find_cached(
        self, verification_tuple: VerificationTuple, version_file_mtime_ns: int
    ) -> PreflightEvidence | None:
        document = self._load()
        entries = document["entries"]
        for raw_entry in entries:
            if not isinstance(raw_entry, Mapping):
                raise PreflightCacheError("preflight cache entry must be an object")
            raw_evidence = raw_entry.get("evidence")
            if not isinstance(raw_evidence, Mapping):
                raise PreflightCacheError("preflight cache entry has no evidence object")
            evidence = PreflightEvidence.from_dict(raw_evidence)
            if (
                evidence.verification_tuple == verification_tuple
                and evidence.version_file_mtime_ns == version_file_mtime_ns
                and evidence.instance_fingerprint == self.instance_fingerprint
            ):
                return evidence
        return None

    def _load(self) -> dict[str, object]:
        if not self.cache_path.exists():
            return {"schema_version": self.CACHE_SCHEMA_VERSION, "entries": []}
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PreflightCacheError(
                f"preflight cache could not be read: {self.cache_path}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != self.CACHE_SCHEMA_VERSION:
            raise PreflightCacheError("preflight cache schema version is unsupported")
        entries = raw.get("entries")
        if not isinstance(entries, list):
            raise PreflightCacheError("preflight cache entries must be a list")
        return {"schema_version": self.CACHE_SCHEMA_VERSION, "entries": entries}

    def _store(self, evidence: PreflightEvidence) -> None:
        document = self._load()
        entries = [
            entry
            for entry in document["entries"]
            if isinstance(entry, Mapping)
            and not _same_verification_tuple(entry.get("evidence"), evidence.verification_tuple)
        ]
        entries.append(
            {
                "verification_tuple": asdict(evidence.verification_tuple),
                "version_file_mtime_ns": evidence.version_file_mtime_ns,
                "evidence": evidence.to_dict(),
            }
        )
        document = {"schema_version": self.CACHE_SCHEMA_VERSION, "entries": entries}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.cache_path.parent,
                prefix=f".{self.cache_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(document, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.cache_path)
        except OSError as exc:
            raise PreflightCacheError(
                f"preflight cache could not be written atomically: {self.cache_path}"
            ) from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass


def _same_verification_tuple(value: object, expected: VerificationTuple) -> bool:
    if not isinstance(value, Mapping):
        return False
    return (
        value.get("cli_version") == expected.cli_version
        and value.get("sandbox_mode") == expected.sandbox_mode
        and value.get("environment_fingerprint") == expected.environment_fingerprint
    )


def _nonblank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreflightContractError(f"{label} must be a non-blank string")
    return value.strip()


def _compare_versions(left: str, right: str) -> int:
    def parts(value: str) -> tuple[int, ...]:
        result: list[int] = []
        for item in value.split("."):
            numeric = item.split("-", 1)[0]
            if not numeric.isdigit():
                raise PreflightContractError(f"CLI version is not numeric: {value}")
            result.append(int(numeric))
        return tuple(result)

    left_parts = parts(left)
    right_parts = parts(right)
    width = max(len(left_parts), len(right_parts))
    padded_left = left_parts + (0,) * (width - len(left_parts))
    padded_right = right_parts + (0,) * (width - len(right_parts))
    return (padded_left > padded_right) - (padded_left < padded_right)


__all__ = [
    "CliVersion",
    "CliVersionReader",
    "DeclarationUpdateEvent",
    "PreflightCacheError",
    "PreflightContractError",
    "PreflightError",
    "PreflightEvidence",
    "PreflightGate",
    "PreflightObservation",
    "PreflightResult",
    "PreflightRunner",
    "VerificationTuple",
    "VersionReadError",
    "candidate_metadata_updater",
]
