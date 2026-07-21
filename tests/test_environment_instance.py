from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vsm.environment_instance import (
    EnvironmentInstance,
    EnvironmentInstanceService,
    EnvironmentInstanceState,
)
from vsm.dispatcher import DependencyAwareDispatcher
from vsm.errors import InvariantViolation
from vsm.kernel.ledger import InMemoryOperationalLedger
from vsm.routing.bayesian import BayesianRouter


SPACE_ID = "space:environment"
ENVIRONMENT_FINGERPRINT = "a" * 64
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class Contract:
    def __init__(self, environment_fingerprint: str) -> None:
        self.environment_fingerprint = environment_fingerprint


CONTRACT = Contract(ENVIRONMENT_FINGERPRINT)


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


def test_instance_binding_does_not_change_contract_fingerprint() -> None:
    first = instance("environment-instance:first", "/srv/workspace-a")
    second = instance("environment-instance:second", "/srv/workspace-b")

    assert first.environment_fingerprint == second.environment_fingerprint
    assert first.instance_fingerprint != second.instance_fingerprint


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
        contract_conformance_passed=True,
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


def test_failed_contract_verification_is_recorded_and_fails_fast() -> None:
    ledger = InMemoryOperationalLedger(SPACE_ID)
    lifecycle = service(ledger)
    candidate = instance("environment-instance:rejected", "/srv/rejected")
    lifecycle.register(
        candidate,
        contract=CONTRACT,
        idempotency_key="environment:rejected:register",
    )

    with pytest.raises(InvariantViolation, match="contract conformance"):
        lifecycle.verify(
            candidate.instance_id,
            contract_conformance_passed=False,
            idempotency_key="environment:rejected:verify",
        )

    history = ledger.stream(candidate.instance_id, after_stream_version=0, limit=20)
    assert history[-1].event.event_type == "environment_instance_verification_failed"
    assert lifecycle.instances[candidate.instance_id].state is EnvironmentInstanceState.CANDIDATE


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
            contract_conformance_passed=True,
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
        contract_conformance_passed=True,
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
