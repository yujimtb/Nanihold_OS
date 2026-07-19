from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vsm.config import LoadedConfig, load_config
from vsm.dispatcher import DependencyAwareDispatcher
from vsm.activation.reorientation import ReorientationService
from vsm.interface.pilot_host import PilotHostInterfacePilot
from vsm.interface.service import InterfaceService
from vsm.kernel.service import Kernel, utc_now
from vsm.lethe.client import LetheHistoryClient, LetheOperationalLedger
from vsm.pilot.host import PilotHostCoordinator
from vsm.pilot.models import DeviceIdentity
from vsm.pilot.production_host import (
    ProductionPilotHostClient,
    WorkExecutionProfile,
)
from vsm.projection import OperationalProjection
from vsm.routing.bayesian import BayesianRouter, RoutingEvidenceService
from vsm.token_lab.lab import TokenEfficiencyLab, TokenLabEventService
from vsm.web.app import AppState, create_app


@dataclass
class Runtime:
    loaded: LoadedConfig
    ledger: LetheOperationalLedger
    history_reader: LetheHistoryClient
    kernel: Kernel
    interface: InterfaceService
    dispatcher: DependencyAwareDispatcher
    projection: OperationalProjection
    state: AppState

    def close(self) -> None:
        self.dispatcher.close()
        self.interface.pilot.close()
        self.history_reader.close()
        self.ledger.close()


def bootstrap(config_path: Path, *, require_active_route: bool = True) -> Runtime:
    loaded = load_config(config_path)
    config = loaded.config
    ledger = LetheOperationalLedger(
        base_url=config.kernel.lethe.base_url,
        bearer_token=loaded.lethe_bearer_token,
        data_space_id=config.kernel.data_space.data_space_id,
        timeout_seconds=config.kernel.lethe.timeout_seconds,
        max_page_size=config.kernel.lethe.max_page_size,
    )
    history_reader = LetheHistoryClient(
        base_url=config.kernel.lethe.base_url,
        bearer_token=loaded.lethe_bearer_token,
        data_space_id=config.kernel.data_space.data_space_id,
        timeout_seconds=config.kernel.lethe.timeout_seconds,
        max_result_bytes=config.kernel.lethe.history_max_result_bytes,
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
    identity = DeviceIdentity(
        pilot_host_id=config.pilot.pilot_host_id,
        device_id=config.pilot.device_id,
        certificate_sha256=config.pilot.device_certificate_sha256,
    )
    production_config = config.production_pilot_host
    if config.deployment.mode == "production":
        if production_config is None:
            raise RuntimeError("production PilotHost configuration is missing")
        coding_candidates = [
            candidate
            for candidate in registry.values()
            if candidate.model_snapshot
            == production_config.coding_candidate_model_snapshot
        ]
        if len(coding_candidates) != 1:
            raise RuntimeError(
                "production coding candidate does not resolve exactly once"
            )
        pilot = ProductionPilotHostClient(
            base_url=interface_config.pilot_host_base_url,
            bearer_token=loaded.pilot_host_bearer_token,
            identity=identity,
            interface_candidate=interface_candidate,
            coding_candidate=coding_candidates[0],
            permission_mode=config.pilot.mode,
            interface_max_budget_usd=(
                production_config.interface_max_budget_usd
            ),
            interface_timeout_seconds=interface_config.timeout_seconds,
            work_profile=WorkExecutionProfile(
                cwd=production_config.work_cwd,
                sandbox=production_config.work_sandbox,
                max_input_tokens=production_config.work_max_input_tokens,
                max_output_tokens=production_config.work_max_output_tokens,
                max_total_tokens=production_config.work_max_total_tokens,
                timeout_seconds=production_config.work_timeout_seconds,
            ),
            transport_timeout_seconds=(
                production_config.transport_timeout_seconds
            ),
        )
    else:
        pilot = PilotHostInterfacePilot(
            candidate=interface_candidate,
            base_url=interface_config.pilot_host_base_url,
            bearer_token=loaded.pilot_host_bearer_token,
            timeout_seconds=interface_config.timeout_seconds,
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
    interface = InterfaceService(
        kernel=kernel,
        ledger=ledger,
        pilot=pilot,
        token_lab_events=token_lab_events,
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
            pilot.close()
            history_reader.close()
            ledger.close()
            raise RuntimeError(
                "configured active RouteSnapshot is missing or unpublished in LETHE"
            )
        registry_keys = frozenset(registry)
        if not frozenset(stored_snapshot.candidate_keys).issubset(registry_keys):
            pilot.close()
            history_reader.close()
            ledger.close()
            raise RuntimeError(
                "active RouteSnapshot references an unregistered ModelCandidate"
            )
        if stored_snapshot.evidence_cursor != routing_evidence.evidence_cursor:
            pilot.close()
            history_reader.close()
            ledger.close()
            raise RuntimeError(
                "active RouteSnapshot evidence cursor is stale"
            )
    dispatcher = DependencyAwareDispatcher(
        kernel=kernel,
        router=router,
        evidence_cursor=lambda: routing_evidence.evidence_cursor,
        model_registry=registry if production_config is not None else None,
        work_executor=pilot if production_config is not None else None,
        max_parallelism=(
            production_config.max_parallelism
            if production_config is not None
            else None
        ),
    )
    reorientation = (
        ReorientationService(
            kernel=kernel,
            interface=interface,
            pilot=pilot,
            history_reader=history_reader,
            max_result_bytes=config.kernel.lethe.history_max_result_bytes,
        )
        if production_config is not None
        else None
    )
    state = AppState(
        kernel=kernel,
        interface=interface,
        pilot_hosts=PilotHostCoordinator(
            kernel,
            expected_identity=identity,
        ),
        router=router,
        routing_evidence=routing_evidence,
        token_lab=token_lab,
        token_lab_events=token_lab_events,
        model_registry=registry,
        api_bearer_token=loaded.api_bearer_token,
        authorized_device_ids=frozenset(config.server.authorized_device_ids),
        dispatcher=dispatcher,
        reorientation_service=reorientation,
        reorientation_max_tool_rounds=(
            production_config.reorientation_max_tool_rounds
            if production_config is not None
            else None
        ),
        coding_pilot_id=(
            production_config.coding_pilot_id
            if production_config is not None
            else None
        ),
        owner_session_lifetime_seconds=config.server.owner_session_lifetime_seconds,
        history_reader=history_reader,
        history_max_result_bytes=config.kernel.lethe.history_max_result_bytes,
    )
    return Runtime(
        loaded=loaded,
        ledger=ledger,
        history_reader=history_reader,
        kernel=kernel,
        interface=interface,
        dispatcher=dispatcher,
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
