from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vsm.environment import EnvironmentContract, environment_fingerprint
from vsm.environment_instance import (
    EnvironmentInstance,
    EnvironmentInstanceService,
    EnvironmentInstanceState,
)
from vsm.dispatcher import DependencyAwareDispatcher
from vsm.errors import InvariantViolation
from vsm.kernel.ledger import InMemoryOperationalLedger
from vsm.preflight import (
    CliVersionReader,
    PreflightEvidence,
    PreflightGate,
    VerificationTuple,
)
from vsm.routing.bayesian import BayesianRouter


SPACE_ID = "space:environment"
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

CONTRACT = EnvironmentContract(
    supported_shells=("posix",),
    required_endpoints=("api.openai.com",),
    workspace_writable=True,
    minimum_memory_mb=1024,
    supported_sandboxes=("workspace-write",),
    required_sandbox="workspace-write",
    path_mapping_names=("workspace-root",),
    minimum_cli_version="0.145.0",
)
ENVIRONMENT_FINGERPRINT = environment_fingerprint(CONTRACT)


def instance(instance_id: str, workspace: str) -> EnvironmentInstance:
    return EnvironmentInstance.from_contract(
        CONTRACT,
        instance_id=instance_id,
        data_space_id=SPACE_ID,
        logical_path_bindings={"workspace-root": workspace},
        cli_executable_path=f"{workspace}/bin/codex",
        codex_home=f"{workspace}/.codex",
        machine_identity={"host": instance_id},
    )


def service(
    ledger: InMemoryOperationalLedger,
    *,
    reprovisioner=None,
) -> EnvironmentInstanceService:
    return EnvironmentInstanceService(
        data_space_id=SPACE_ID,
        ledger=ledger,
        clock=lambda: NOW,
        reprovisioner=reprovisioner,
    )


def evidence(candidate: EnvironmentInstance) -> PreflightEvidence:
    return PreflightEvidence(
        verification_tuple=VerificationTuple(
            cli_version="0.145.0",
            sandbox_mode="workspace-write",
            environment_fingerprint=ENVIRONMENT_FINGERPRINT,
        ),
        measured_sandbox_policy="workspace-write",
        measured_capabilities={
            "workspace_writable": True,
            "endpoint_reachable": ["api.openai.com"],
            "memory_bytes": 2 * 1024 * 1024 * 1024,
            "shell": "posix",
            "path_mappings": ["workspace-root"],
        },
        instance_fingerprint=candidate.instance_fingerprint,
        checked_at=NOW.isoformat(),
        rollout_ref="rollout:environment-instance",
        version_file="/opt/codex/package.json",
        version_file_mtime_ns=1,
    )


def test_instance_binding_does_not_change_contract_fingerprint() -> None:
    first = instance("environment-instance:first", "/srv/workspace-a")
    second = instance("environment-instance:second", "/srv/workspace-b")

    assert first.environment_fingerprint == second.environment_fingerprint
    assert first.instance_fingerprint != second.instance_fingerprint


def test_instance_requires_every_contract_logical_path_binding() -> None:
    with pytest.raises(InvariantViolation, match="exactly match"):
        EnvironmentInstance.from_contract(
            CONTRACT,
            instance_id="environment-instance:missing-path",
            data_space_id=SPACE_ID,
            logical_path_bindings={"different-root": "/srv/workspace"},
            cli_executable_path="/srv/workspace/bin/codex",
            codex_home="/srv/workspace/.codex",
        )


def test_lifecycle_operations_append_instance_fingerprint_events() -> None:
    ledger = InMemoryOperationalLedger(SPACE_ID)
    lifecycle = service(ledger)
    candidate = instance("environment-instance:lifecycle", "/srv/lifecycle")

    registered = lifecycle.register(
        candidate,
        contract=CONTRACT,
        idempotency_key="environment:lifecycle:register",
    )
    verified = lifecycle.verify(
        candidate.instance_id,
        evidence=evidence(candidate),
        idempotency_key="environment:lifecycle:verify",
    )
    activated = lifecycle.activate(
        candidate.instance_id,
        idempotency_key="environment:lifecycle:activate",
    )
    retired = lifecycle.retire(
        candidate.instance_id,
        idempotency_key="environment:lifecycle:retire",
    )

    assert registered.event_type == "environment_instance_registered"
    assert verified.event_type == "environment_instance_verified"
    assert activated.event_type == "environment_instance_activated"
    assert retired.event_type == "environment_instance_retired"
    assert registered.payload["instance_fingerprint"] == candidate.instance_fingerprint
    assert lifecycle.instances[candidate.instance_id].state is EnvironmentInstanceState.RETIRED

    history = ledger.stream(candidate.instance_id, after_stream_version=0, limit=20)
    assert [stored.event.event_type for stored in history] == [
        "environment_instance_registered",
        "environment_instance_verified",
        "environment_instance_activated",
        "environment_instance_retired",
    ]
    assert all(stored.event.actor_type == "system" for stored in history)


def test_mismatched_preflight_evidence_fails_fast_without_promotion() -> None:
    ledger = InMemoryOperationalLedger(SPACE_ID)
    lifecycle = service(ledger)
    candidate = instance("environment-instance:rejected", "/srv/rejected")
    lifecycle.register(
        candidate,
        contract=CONTRACT,
        idempotency_key="environment:rejected:register",
    )

    mismatched = replace(
        evidence(candidate),
        instance_fingerprint="b" * 64,
    )
    with pytest.raises(InvariantViolation, match="instance fingerprint"):
        lifecycle.verify(
            candidate.instance_id,
            evidence=mismatched,
            idempotency_key="environment:rejected:verify",
        )

    history = ledger.stream(candidate.instance_id, after_stream_version=0, limit=20)
    assert history[-1].event.event_type == "environment_instance_registered"
    assert lifecycle.instances[candidate.instance_id].state is EnvironmentInstanceState.CANDIDATE


def test_preflight_evidence_hook_verifies_instance_in_operational_ledger(
    tmp_path: Path,
) -> None:
    ledger = InMemoryOperationalLedger(SPACE_ID)
    lifecycle = service(ledger)
    candidate = instance("environment-instance:preflight", "/srv/preflight")
    lifecycle.register(
        candidate,
        contract=CONTRACT,
        idempotency_key="environment:preflight:register",
    )
    version_file = tmp_path / "package.json"
    version_file.write_text('{"version":"0.145.0"}', encoding="utf-8")
    gate = PreflightGate(
        contract=CONTRACT,
        instance_fingerprint=candidate.instance_fingerprint,
        version_reader=CliVersionReader(version_file),
        cache_path=tmp_path / "preflight-cache.json",
        preflight_runner=lambda _verification: {
            "sandbox_policy": "workspace-write",
            "workspace_writable": True,
            "endpoint_reachable": ["api.openai.com"],
            "memory_bytes": 2 * 1024 * 1024 * 1024,
            "shell": "posix",
            "path_mappings": ["workspace-root"],
        },
        evidence_hook=lifecycle.preflight_evidence_hook(
            candidate.instance_id,
            idempotency_key_prefix="environment:preflight:verify",
        ),
        clock=lambda: NOW,
    )

    result = gate.dispatch_preflight()

    assert result.cache_hit is False
    assert lifecycle.instances[candidate.instance_id].state is EnvironmentInstanceState.VERIFIED
    history = ledger.stream(candidate.instance_id, after_stream_version=0, limit=20)
    verified = history[-1].event
    assert verified.event_type == "environment_instance_verified"
    assert verified.payload["preflight_evidence"] == result.evidence.to_dict()


def test_commissioned_active_instance_can_be_attached_without_replaying_registration() -> None:
    ledger = InMemoryOperationalLedger(SPACE_ID)
    lifecycle = service(ledger)
    candidate = instance("environment-instance:commissioned", "/srv/commissioned")

    attached = lifecycle.attach_active(candidate, contract=CONTRACT)

    assert attached.state is EnvironmentInstanceState.ACTIVE
    assert lifecycle.instances[candidate.instance_id] == attached
    assert ledger.stream(candidate.instance_id, 0, 20) == []


def test_failover_selects_verified_peer_without_changing_environment_fingerprint() -> None:
    ledger = InMemoryOperationalLedger(SPACE_ID)
    lifecycle = service(ledger)
    active = instance("environment-instance:active", "/srv/active")
    replacement = instance("environment-instance:replacement", "/srv/replacement")
    for candidate, key in (
        (active, "active"),
        (replacement, "replacement"),
    ):
        lifecycle.register(
            candidate,
            contract=CONTRACT,
            idempotency_key=f"environment:{key}:register",
        )
        lifecycle.verify(
            candidate.instance_id,
            evidence=evidence(candidate),
            idempotency_key=f"environment:{key}:verify",
        )
    lifecycle.activate(
        active.instance_id,
        idempotency_key="environment:active:activate",
    )

    selected = lifecycle.failover(
        active.instance_id,
        idempotency_key="environment:failover:active-to-replacement",
    )

    assert selected is not None
    assert selected.instance_id == replacement.instance_id
    assert selected.state is EnvironmentInstanceState.ACTIVE
    assert lifecycle.instances[active.instance_id].state is EnvironmentInstanceState.RETIRED
    assert selected.environment_fingerprint == active.environment_fingerprint
    events = [stored.event for stored in ledger.page(0, 100)]
    failover_events = [
        event for event in events if event.event_type == "environment_instance_failed_over"
    ]
    assert len(failover_events) == 1
    assert failover_events[0].payload["failed_instance_id"] == active.instance_id
    assert failover_events[0].payload["replacement_instance_id"] == replacement.instance_id
    assert failover_events[0].payload["environment_fingerprint"] == ENVIRONMENT_FINGERPRINT


def test_failover_requests_reprovisioning_when_no_verified_peer_exists() -> None:
    ledger = InMemoryOperationalLedger(SPACE_ID)
    requests: list[tuple[str, str, EnvironmentInstanceState]] = []

    def request_reprovisioning(**kwargs) -> None:
        failed_instance = kwargs["failed_instance"]
        requests.append(
            (
                kwargs["environment_fingerprint"],
                failed_instance.instance_id,
                failed_instance.state,
            )
        )

    lifecycle = service(ledger, reprovisioner=request_reprovisioning)
    active = instance("environment-instance:only", "/srv/only")
    lifecycle.register(
        active,
        contract=CONTRACT,
        idempotency_key="environment:only:register",
    )
    lifecycle.verify(
        active.instance_id,
        evidence=evidence(active),
        idempotency_key="environment:only:verify",
    )
    lifecycle.activate(
        active.instance_id,
        idempotency_key="environment:only:activate",
    )

    assert (
        lifecycle.failover(
            active.instance_id,
            idempotency_key="environment:failover:reprovision",
        )
        is None
    )
    assert requests == [
        (ENVIRONMENT_FINGERPRINT, active.instance_id, EnvironmentInstanceState.RETIRED)
    ]
    events = [stored.event.event_type for stored in ledger.page(0, 100)]
    assert "environment_instance_reprovision_requested" in events
    assert "environment_instance_failed_over" not in events
    assert "environment_instance_registered" in events


def test_dispatcher_exposes_injected_environment_failover_boundary(system) -> None:
    calls: list[tuple[str, str]] = []

    class Failover:
        def failover(self, failed_instance_id: str, *, idempotency_key: str):
            calls.append((failed_instance_id, idempotency_key))
            return None

    dispatcher = DependencyAwareDispatcher(
        kernel=system[0],
        router=BayesianRouter(
            expected_utility_quality_weight=1,
            expected_utility_cost_weight=0,
            expected_utility_latency_weight=0,
        ),
        evidence_cursor=lambda: 0,
        startup_projection_cursor=0,
        environment_failover=Failover(),
    )

    assert (
        dispatcher.failover_environment(
            failed_instance_id="environment-instance:dispatcher",
            idempotency_key="environment:dispatcher:failover",
        )
        is None
    )
    dispatcher.close()
    assert calls == [
        (
            "environment-instance:dispatcher",
            "environment:dispatcher:failover",
        )
    ]
