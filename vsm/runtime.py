from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vsm.config import LoadedConfig, load_config
from vsm.interface.claude import ClaudeInterfacePilot
from vsm.interface.service import InterfaceService
from vsm.kernel.service import Kernel, utc_now
from vsm.lethe.client import LetheOperationalLedger
from vsm.pilot.host import PilotHostCoordinator
from vsm.pilot.models import DeviceIdentity
from vsm.projection import OperationalProjection
from vsm.routing.bayesian import BayesianRouter, RoutingEvidenceService
from vsm.token_lab.lab import TokenEfficiencyLab, TokenLabEventService
from vsm.web.app import AppState, create_app


@dataclass
class Runtime:
    loaded: LoadedConfig
    ledger: LetheOperationalLedger
    kernel: Kernel
    interface: InterfaceService
    projection: OperationalProjection
    state: AppState

    def close(self) -> None:
        self.ledger.close()


def bootstrap(config_path: Path, *, require_active_route: bool = True) -> Runtime:
    loaded = load_config(config_path)
    config = loaded.config
    ledger = LetheOperationalLedger(
        base_url=config.kernel.lethe.base_url,
        bearer_token=loaded.lethe_bearer_token,
        data_space_id=config.kernel.data_space.data_space_id,
        timeout_seconds=config.kernel.lethe.timeout_seconds,
    )
    kernel = Kernel(
        data_space=config.kernel.data_space,
        ledger=ledger,
        audit_policy=config.kernel.audit_policy,
        control_policy=config.kernel.control_policy,
        clock=utc_now,
    )
    registry = {
        registration.candidate.key: registration.candidate
        for registration in config.routing.candidates
    }
    interface_config = config.interface_pilot
    interface_candidate = next(
        candidate
        for candidate in registry.values()
        if (
            candidate.adapter == interface_config.adapter
            and candidate.adapter_version == interface_config.adapter_version
            and candidate.provider == interface_config.provider
            and candidate.model_snapshot == interface_config.model_snapshot
            and candidate.effort == interface_config.effort
            and candidate.toolset == interface_config.toolset
            and candidate.sandbox_fingerprint
            == interface_config.sandbox_fingerprint
            and candidate.environment_fingerprint
            == interface_config.environment_fingerprint
        )
    )
    pilot = ClaudeInterfacePilot(
        candidate=interface_candidate,
        policy=config.pilot.policy(),
        timeout_seconds=config.kernel.lethe.timeout_seconds,
    )
    interface = InterfaceService(
        kernel=kernel,
        ledger=ledger,
        pilot=pilot,
        clock=utc_now,
    )
    router = BayesianRouter(
        expected_utility_quality_weight=(
            config.routing.expected_utility_quality_weight
        ),
        expected_utility_cost_weight=config.routing.expected_utility_cost_weight,
        expected_utility_latency_weight=(
            config.routing.expected_utility_latency_weight
        ),
    )
    for registration in config.routing.candidates:
        router.register(registration.candidate, registration.priors)
    routing_evidence = RoutingEvidenceService(
        router=router,
        ledger=ledger,
        data_space_id=config.kernel.data_space.data_space_id,
        clock=utc_now,
    )
    token_lab = TokenEfficiencyLab()
    token_lab_events = TokenLabEventService(
        lab=token_lab,
        ledger=ledger,
        data_space_id=config.kernel.data_space.data_space_id,
        clock=utc_now,
    )
    projection = OperationalProjection(
        kernel=kernel,
        interface=interface,
        routing_evidence=routing_evidence,
        token_lab_events=token_lab_events,
    )
    projection.rebuild()
    if require_active_route:
        active_snapshot_id = config.routing.active_route_snapshot_id
        stored_snapshot = kernel.route_snapshots.get(active_snapshot_id)
        if stored_snapshot is None or stored_snapshot.state != "published":
            ledger.close()
            raise RuntimeError(
                "configured active RouteSnapshot is missing or unpublished in LETHE"
            )
        registry_keys = frozenset(registry)
        if not frozenset(stored_snapshot.candidate_keys).issubset(registry_keys):
            ledger.close()
            raise RuntimeError(
                "active RouteSnapshot references an unregistered ModelCandidate"
            )
        if stored_snapshot.evidence_cursor != routing_evidence.evidence_cursor:
            ledger.close()
            raise RuntimeError(
                "active RouteSnapshot evidence cursor is stale"
            )
    state = AppState(
        kernel=kernel,
        interface=interface,
        pilot_hosts=PilotHostCoordinator(
            kernel,
            expected_identity=DeviceIdentity(
                pilot_host_id=config.pilot.pilot_host_id,
                device_id=config.pilot.device_id,
                certificate_sha256=config.pilot.device_certificate_sha256,
            ),
        ),
        router=router,
        routing_evidence=routing_evidence,
        token_lab=token_lab,
        token_lab_events=token_lab_events,
        model_registry=registry,
        api_bearer_token=loaded.api_bearer_token,
    )
    return Runtime(
        loaded=loaded,
        ledger=ledger,
        kernel=kernel,
        interface=interface,
        projection=projection,
        state=state,
    )


def build_app(config_path: Path):
    runtime = bootstrap(config_path)
    app = create_app(
        runtime.state,
        allowed_origins=runtime.loaded.config.server.allowed_origins,
    )
    app.state.nanihold_runtime = runtime

    @app.on_event("shutdown")
    def close_runtime() -> None:
        runtime.close()

    return app
