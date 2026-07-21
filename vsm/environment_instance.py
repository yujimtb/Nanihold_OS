"""Environment contract bindings, lifecycle, and failover.

An :class:`EnvironmentInstance` is the concrete execution location for an
environment contract.  The contract fingerprint is deliberately supplied by
the contract layer; machine-specific bindings are used only for the instance
fingerprint.

Provisioning and preflight execution are intentionally outside this module's
scope.  Callers report the preflight result to :class:`EnvironmentInstanceService`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import Field, model_validator

from vsm.errors import InvariantViolation
from vsm.ids import deterministic_event_id
from vsm.kernel.ledger import OperationalLedger
from vsm.kernel.models import EventEnvelope, Identifier, NonBlank, StrictModel


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
Sha256 = str


class EnvironmentContractLike(Protocol):
    """Minimal contract surface needed by an environment instance.

    Track A 統合時に差し替え: Track A の正式な EnvironmentContract 実装を
    利用できるようになった時点で、この暫定 Protocol を置き換える。
    """

    @property
    def environment_fingerprint(self) -> str:
        """Return the normalized, contract-only environment fingerprint."""


class EnvironmentInstanceState(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    ACTIVE = "active"
    RETIRED = "retired"


def compute_instance_fingerprint(
    *,
    logical_path_bindings: Mapping[str, str],
    cli_executable_path: str,
    codex_home: str,
    environment_variables: Mapping[str, str],
    machine_identity: Mapping[str, str],
) -> str:
    """Hash only concrete runtime information for an instance identity.

    ``environment_fingerprint`` and ``instance_id`` are intentionally absent
    from this canonical document.  In particular, changing the concrete
    execution location cannot change the candidate identity.
    """

    canonical = json.dumps(
        {
            "cli_executable_path": cli_executable_path,
            "codex_home": codex_home,
            "environment_variables": dict(environment_variables),
            "logical_path_bindings": dict(logical_path_bindings),
            "machine_identity": dict(machine_identity),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_sha256(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise InvariantViolation(f"{field_name} must be a lowercase SHA-256 fingerprint")
    return value


def _contract_fingerprint(contract: EnvironmentContractLike) -> str:
    return _require_sha256(
        contract.environment_fingerprint,
        field_name="EnvironmentContract.environment_fingerprint",
    )


class EnvironmentInstance(StrictModel):
    """A concrete execution location bound to a portable contract."""

    instance_id: Identifier
    data_space_id: Identifier
    environment_fingerprint: Sha256 = Field(pattern=SHA256_PATTERN.pattern)
    logical_path_bindings: dict[str, str] = Field(min_length=1)
    cli_executable_path: NonBlank
    codex_home: NonBlank
    environment_variables: dict[str, str] = Field(default_factory=dict)
    machine_identity: dict[str, str] = Field(default_factory=dict)
    instance_fingerprint: Sha256 = Field(pattern=SHA256_PATTERN.pattern)
    state: EnvironmentInstanceState = EnvironmentInstanceState.CANDIDATE

    @model_validator(mode="after")
    def fingerprint_matches_runtime_binding(self) -> "EnvironmentInstance":
        expected = compute_instance_fingerprint(
            logical_path_bindings=self.logical_path_bindings,
            cli_executable_path=self.cli_executable_path,
            codex_home=self.codex_home,
            environment_variables=self.environment_variables,
            machine_identity=self.machine_identity,
        )
        if self.instance_fingerprint != expected:
            raise ValueError(
                "instance_fingerprint must match the concrete runtime binding"
            )
        return self

    @classmethod
    def from_contract(
        cls,
        contract: EnvironmentContractLike,
        *,
        instance_id: str,
        data_space_id: str,
        logical_path_bindings: Mapping[str, str],
        cli_executable_path: str,
        codex_home: str,
        environment_variables: Mapping[str, str] | None = None,
        machine_identity: Mapping[str, str] | None = None,
    ) -> "EnvironmentInstance":
        """Bind a contract to a concrete location without changing its identity."""

        if environment_variables is None:
            environment_variables = {}
        if machine_identity is None:
            machine_identity = {}
        instance_fingerprint = compute_instance_fingerprint(
            logical_path_bindings=logical_path_bindings,
            cli_executable_path=cli_executable_path,
            codex_home=codex_home,
            environment_variables=environment_variables,
            machine_identity=machine_identity,
        )
        return cls(
            instance_id=instance_id,
            data_space_id=data_space_id,
            environment_fingerprint=_contract_fingerprint(contract),
            logical_path_bindings=dict(logical_path_bindings),
            cli_executable_path=cli_executable_path,
            codex_home=codex_home,
            environment_variables=dict(environment_variables),
            machine_identity=dict(machine_identity),
            instance_fingerprint=instance_fingerprint,
        )


@runtime_checkable
class EnvironmentReprovisioner(Protocol):
    """Boundary hook for a later provisioning implementation."""

    def request_reprovision(
        self,
        *,
        environment_fingerprint: str,
        failed_instance: EnvironmentInstance,
    ) -> None: ...


ReprovisionerCallback = Callable[..., None]


class EnvironmentInstanceService:
    """Manage EnvironmentInstance state and its Operational Ledger history."""

    def __init__(
        self,
        *,
        data_space_id: str,
        ledger: OperationalLedger,
        clock: Callable[[], datetime],
        reprovisioner: EnvironmentReprovisioner | ReprovisionerCallback | None = None,
    ) -> None:
        self.data_space_id = data_space_id
        self.ledger = ledger
        self.clock = clock
        self.reprovisioner = reprovisioner
        self.instances: dict[str, EnvironmentInstance] = {}
        self._versions: dict[str, int] = {}

    def _record(
        self,
        *,
        stream_id: str,
        event_type: str,
        payload: dict[str, object],
        idempotency_key: str,
    ) -> EventEnvelope:
        expected = self._versions.get(stream_id, 0)
        event = EventEnvelope(
            event_id=deterministic_event_id(
                data_space_id=self.data_space_id,
                stream_id=stream_id,
                idempotency_key=idempotency_key,
            ),
            data_space_id=self.data_space_id,
            stream_id=stream_id,
            stream_version=expected + 1,
            event_type=event_type,
            occurred_at=self.clock(),
            actor_type="system",
            actor_id=None,
            correlation_id=stream_id,
            causation_id=None,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        result = self.ledger.append(event, expected)
        self._versions[stream_id] = result.stream_version
        return event

    @staticmethod
    def _contract_stream_id(environment_fingerprint: str) -> str:
        return f"environment-contract:{environment_fingerprint}"

    def _require_instance(self, instance_id: str) -> EnvironmentInstance:
        try:
            return self.instances[instance_id]
        except KeyError as exc:
            raise InvariantViolation(f"EnvironmentInstance not found: {instance_id}") from exc

    def _validate_instance_contract(
        self, instance: EnvironmentInstance, contract: EnvironmentContractLike
    ) -> None:
        if instance.data_space_id != self.data_space_id:
            raise InvariantViolation("EnvironmentInstance DataSpace mismatch")
        if instance.environment_fingerprint != _contract_fingerprint(contract):
            raise InvariantViolation(
                "EnvironmentInstance does not match the EnvironmentContract"
            )

    @staticmethod
    def _serialized(instance: EnvironmentInstance) -> dict[str, object]:
        return instance.model_dump(mode="json")

    def register(
        self,
        instance: EnvironmentInstance,
        *,
        contract: EnvironmentContractLike,
        idempotency_key: str,
    ) -> EventEnvelope:
        """Record a newly discovered candidate with its instance fingerprint."""

        self._validate_instance_contract(instance, contract)
        if instance.state is not EnvironmentInstanceState.CANDIDATE:
            raise InvariantViolation("new EnvironmentInstance must be a candidate")
        if instance.instance_id in self.instances:
            raise InvariantViolation(
                f"EnvironmentInstance already exists: {instance.instance_id}"
            )
        event = self._record(
            stream_id=instance.instance_id,
            event_type="environment_instance_registered",
            payload={
                "instance": self._serialized(instance),
                "environment_fingerprint": instance.environment_fingerprint,
                "instance_fingerprint": instance.instance_fingerprint,
            },
            idempotency_key=idempotency_key,
        )
        self.instances[instance.instance_id] = instance
        return event

    def verify(
        self,
        instance_id: str,
        *,
        contract_conformance_passed: bool,
        idempotency_key: str,
    ) -> EventEnvelope:
        """Record the contract preflight result and promote a passing candidate."""

        instance = self._require_instance(instance_id)
        if instance.state is not EnvironmentInstanceState.CANDIDATE:
            raise InvariantViolation("only a candidate EnvironmentInstance can be verified")
        if not contract_conformance_passed:
            self._record(
                stream_id=instance.instance_id,
                event_type="environment_instance_verification_failed",
                payload={
                    "instance_id": instance.instance_id,
                    "environment_fingerprint": instance.environment_fingerprint,
                    "instance_fingerprint": instance.instance_fingerprint,
                    "contract_conformance_passed": False,
                },
                idempotency_key=idempotency_key,
            )
            raise InvariantViolation(
                "EnvironmentInstance failed the contract conformance test"
            )
        verified = instance.model_copy(
            update={"state": EnvironmentInstanceState.VERIFIED}
        )
        event = self._record(
            stream_id=instance.instance_id,
            event_type="environment_instance_verified",
            payload={
                "instance": self._serialized(verified),
                "contract_conformance_passed": True,
            },
            idempotency_key=idempotency_key,
        )
        self.instances[instance_id] = verified
        return event

    def activate(self, instance_id: str, *, idempotency_key: str) -> EventEnvelope:
        instance = self._require_instance(instance_id)
        if instance.state is not EnvironmentInstanceState.VERIFIED:
            raise InvariantViolation(
                "only a verified EnvironmentInstance can be activated"
            )
        active = instance.model_copy(update={"state": EnvironmentInstanceState.ACTIVE})
        event = self._record(
            stream_id=instance.instance_id,
            event_type="environment_instance_activated",
            payload={"instance": self._serialized(active)},
            idempotency_key=idempotency_key,
        )
        self.instances[instance_id] = active
        return event

    def retire(self, instance_id: str, *, idempotency_key: str) -> EventEnvelope:
        instance = self._require_instance(instance_id)
        if instance.state is EnvironmentInstanceState.RETIRED:
            raise InvariantViolation("EnvironmentInstance is already retired")
        retired = instance.model_copy(update={"state": EnvironmentInstanceState.RETIRED})
        event = self._record(
            stream_id=instance.instance_id,
            event_type="environment_instance_retired",
            payload={"instance": self._serialized(retired)},
            idempotency_key=idempotency_key,
        )
        self.instances[instance_id] = retired
        return event

    def verified_instances(self, environment_fingerprint: str) -> tuple[EnvironmentInstance, ...]:
        _require_sha256(
            environment_fingerprint,
            field_name="environment_fingerprint",
        )
        return tuple(
            sorted(
                (
                    instance
                    for instance in self.instances.values()
                    if (
                        instance.environment_fingerprint == environment_fingerprint
                        and instance.state is EnvironmentInstanceState.VERIFIED
                    )
                ),
                key=lambda instance: instance.instance_id,
            )
        )

    def failover(
        self,
        failed_instance_id: str,
        *,
        idempotency_key: str,
    ) -> EnvironmentInstance | None:
        """Switch to a verified peer or request autonomous reprovisioning.

        No owner approval is consulted here.  The injected reprovisioner is a
        request-only boundary; constructing and verifying a new machine is a
        separate implementation track.
        """

        failed = self._require_instance(failed_instance_id)
        if failed.state is not EnvironmentInstanceState.ACTIVE:
            raise InvariantViolation("failover requires an active EnvironmentInstance")
        candidates = tuple(
            instance
            for instance in self.verified_instances(failed.environment_fingerprint)
            if instance.instance_id != failed_instance_id
        )
        contract_stream_id = self._contract_stream_id(
            failed.environment_fingerprint
        )
        if candidates:
            replacement = candidates[0]
            replacement_active = replacement.model_copy(
                update={"state": EnvironmentInstanceState.ACTIVE}
            )
            failed_retired = failed.model_copy(
                update={"state": EnvironmentInstanceState.RETIRED}
            )
            self._record(
                stream_id=contract_stream_id,
                event_type="environment_instance_failed_over",
                payload={
                    "environment_fingerprint": failed.environment_fingerprint,
                    "failed_instance_id": failed.instance_id,
                    "failed_instance_fingerprint": failed.instance_fingerprint,
                    "replacement_instance_id": replacement.instance_id,
                    "replacement_instance_fingerprint": replacement.instance_fingerprint,
                },
                idempotency_key=idempotency_key,
            )
            self._record(
                stream_id=failed.instance_id,
                event_type="environment_instance_retired",
                payload={"instance": self._serialized(failed_retired)},
                idempotency_key=f"{idempotency_key}:retire",
            )
            self._record(
                stream_id=replacement.instance_id,
                event_type="environment_instance_activated",
                payload={"instance": self._serialized(replacement_active)},
                idempotency_key=f"{idempotency_key}:activate",
            )
            self.instances[failed.instance_id] = failed_retired
            self.instances[replacement.instance_id] = replacement_active
            return replacement_active

        failed_retired = failed.model_copy(
            update={"state": EnvironmentInstanceState.RETIRED}
        )
        self._record(
            stream_id=failed.instance_id,
            event_type="environment_instance_retired",
            payload={"instance": self._serialized(failed_retired)},
            idempotency_key=f"{idempotency_key}:retire",
        )
        self._record(
            stream_id=contract_stream_id,
            event_type="environment_instance_reprovision_requested",
            payload={
                "environment_fingerprint": failed.environment_fingerprint,
                "failed_instance_id": failed.instance_id,
                "failed_instance_fingerprint": failed.instance_fingerprint,
            },
            idempotency_key=idempotency_key,
        )
        self.instances[failed.instance_id] = failed_retired
        self._request_reprovision(failed_retired)
        return None

    def _request_reprovision(self, failed_instance: EnvironmentInstance) -> None:
        if self.reprovisioner is None:
            raise InvariantViolation(
                "no reprovisioner hook is configured for EnvironmentInstance failover"
            )
        if isinstance(self.reprovisioner, EnvironmentReprovisioner):
            self.reprovisioner.request_reprovision(
                environment_fingerprint=failed_instance.environment_fingerprint,
                failed_instance=failed_instance,
            )
            return
        if callable(self.reprovisioner):
            self.reprovisioner(
                environment_fingerprint=failed_instance.environment_fingerprint,
                failed_instance=failed_instance,
            )
            return
        raise InvariantViolation("invalid reprovisioner hook")
