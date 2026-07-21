from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path

import httpx
import typer

from vsm.activation.models import ReorientationRevisionReason
from vsm.errors import ConfigurationError, InvariantViolation
from vsm.config import load_config
from vsm.interface.models import Conversation, SurfaceBinding
from vsm.migration.legacy import (
    MigrationPlan,
    archive_legacy,
    build_plan,
    import_plan,
    scan_legacy,
)
from vsm.runtime import bootstrap, build_app
from vsm.tui import load_operational_snapshot, render_dashboard
from vsm.kernel.models import (
    NodeKind,
    NodeStatus,
    RouteSnapshot,
    RouteSnapshotState,
    UVSMNode,
    VSMFunction,
)
from vsm.routing.bayesian import require_coding_route_candidate_keys

app = typer.Typer(
    name="vsm",
    help="Nanihold Node/Work/Event control plane.",
    no_args_is_help=True,
)
events_app = typer.Typer(help="Read the canonical Event Ledger.")
migration_app = typer.Typer(help="One-time legacy archive migration.")
routes_app = typer.Typer(help="Commission and inspect approved Bayesian routes.")
verification_app = typer.Typer(
    help="Commission the isolated local verification environment."
)
reorientation_app = typer.Typer(help="Start and confirm Interface reorientation.")
app.add_typer(events_app, name="events")
app.add_typer(migration_app, name="migration")
app.add_typer(routes_app, name="routes")
app.add_typer(verification_app, name="verification")
app.add_typer(reorientation_app, name="reorientation")


@app.command("owner-bootstrap")
def owner_bootstrap(
    config: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
    base_url: str = typer.Option(..., min=1),
    lifetime_seconds: int = typer.Option(..., min=1, max=900),
    idempotency_key: str = typer.Option(..., min=1),
) -> None:
    """Issue a one-time, short-lived owner bootstrap link."""
    runtime = bootstrap(config, require_active_route=False)
    try:
        grant = runtime.kernel.owner_bootstrap.issue(
            base_url=base_url,
            lifetime_seconds=lifetime_seconds,
            idempotency_key=idempotency_key,
        )
        _json(grant)
    finally:
        runtime.close()


class InspectResource(StrEnum):
    DATA_SPACES = "data-spaces"
    NODES = "nodes"
    WORK_ITEMS = "work-items"
    EXECUTIONS = "executions"
    EVENTS = "events"
    CONVERSATIONS = "conversations"
    PILOT_HOSTS = "pilot-hosts"
    MODEL_REGISTRY = "model-registry"
    ROUTE_SNAPSHOTS = "route-snapshots"
    TOKEN_LAB = "token-lab"


def _json(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


_TRACE_PAGE_SIZE = 1000


def _event_mentions_execution(event: object, execution_id: str) -> bool:
    if event.stream_id == execution_id or event.correlation_id == execution_id:
        return True
    payload = event.payload
    if payload.get("execution_id") == execution_id:
        return True
    execution = payload.get("execution")
    return (
        isinstance(execution, dict)
        and execution.get("execution_id") == execution_id
    )


def _execution_trace(runtime: object, execution_id: str) -> dict[str, object]:
    execution = runtime.kernel.executions.get(execution_id)
    if execution is None:
        raise typer.BadParameter(f"execution_id not found: {execution_id}")

    timeline: list[dict[str, object]] = []
    cursor = 0
    while True:
        page = runtime.kernel.ledger.page(cursor, _TRACE_PAGE_SIZE)
        if not page:
            break
        page_cursor = cursor
        for stored in page:
            if stored.cursor <= page_cursor:
                raise InvariantViolation(
                    "Event Ledger trace cursor did not advance"
                )
            page_cursor = stored.cursor
            event = stored.event
            if not _event_mentions_execution(event, execution_id):
                continue
            event_json = event.model_dump(mode="json")
            payload = event_json["payload"]
            entry: dict[str, object] = {
                "cursor": stored.cursor,
                "occurred_at": event_json["occurred_at"],
                "event_type": event.event_type,
                "event": event_json,
            }
            provider_session_id = payload.get("provider_session_id")
            if event.event_type == "execution_created":
                created_execution = payload["execution"]
                if not isinstance(created_execution, dict):
                    raise InvariantViolation(
                        "execution_created payload has an invalid Execution"
                    )
                provider_session_id = created_execution["provider_session_id"]
            if provider_session_id is not None:
                entry["provider_session_id"] = provider_session_id
            if event.event_type == "execution_created":
                entry["kind"] = "dispatch"
            elif event.event_type == "pilot_execution_receipt_recorded":
                entry["kind"] = "receipt"
                entry["receipt"] = {
                    "receipt_id": payload["receipt_id"],
                    "status": payload["receipt_status"],
                    "requested_model": payload["requested_model"],
                    "actual_model": payload["actual_model"],
                    "provider_session_id": payload["provider_session_id"],
                    "usage": payload["usage"],
                    "result": payload["result"],
                    "error": payload["error"],
                }
            else:
                entry["kind"] = "ledger_event"
            timeline.append(entry)
        cursor = page_cursor

    receipt = next(
        (
            item["receipt"]
            for item in reversed(timeline)
            if item["kind"] == "receipt"
        ),
        None,
    )
    provider_session_id_refs = [
        {
            "cursor": item["cursor"],
            "occurred_at": item["occurred_at"],
            "event_type": item["event_type"],
            "provider_session_id": item["provider_session_id"],
        }
        for item in timeline
        if "provider_session_id" in item
        and item["provider_session_id"] is not None
    ]
    return {
        "execution_id": execution_id,
        "execution": execution.model_dump(mode="json"),
        "timeline": timeline,
        "receipt": receipt,
        "provider_session_id": execution.provider_session_id,
        "provider_session_id_refs": provider_session_id_refs,
    }


@app.command()
def serve(
    config: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Serve the REST/WebSocket interface from an explicit configuration."""
    import uvicorn

    web_app = build_app(config)
    server = web_app.state.nanihold_runtime.loaded.config.server
    uvicorn.run(
        web_app,
        host=server.bind_host,
        port=server.bind_port,
        workers=1,
    )


@routes_app.command("models")
def route_models(
    config: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Print exact ModelCandidate keys from explicit configuration."""
    loaded = load_config(config)
    _json(
        [
            {
                "key": registration.candidate.key,
                "candidate": registration.candidate.model_dump(mode="json"),
            }
            for registration in loaded.config.routing.candidates
        ]
    )


@routes_app.command("publish")
def route_publish(
    config: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
    route_key: str = typer.Option(..., min=1),
    evidence_cursor: int = typer.Option(..., min=0),
    candidate_key: list[str] = typer.Option(..., "--candidate-key"),
    objective: str = typer.Option(...),
    s3_star_actor_id: str = typer.Option(...),
    owner_actor_id: str = typer.Option(...),
    idempotency_prefix: str = typer.Option(..., min=1),
) -> None:
    """Publish the configured RouteSnapshot through ordered S3*/owner approval."""
    runtime = bootstrap(config, require_active_route=False)
    try:
        if not candidate_key:
            raise typer.BadParameter("at least one --candidate-key is required")
        if evidence_cursor != runtime.state.routing_evidence.evidence_cursor:
            raise typer.BadParameter(
                "evidence cursor must equal the current verified evidence cursor "
                f"({runtime.state.routing_evidence.evidence_cursor})"
            )
        snapshot_id = runtime.loaded.config.routing.active_route_snapshot_id
        if snapshot_id in runtime.kernel.route_snapshots:
            raise typer.BadParameter(f"RouteSnapshot already exists: {snapshot_id}")
        unknown = sorted(set(candidate_key) - set(runtime.state.model_registry))
        if unknown:
            raise typer.BadParameter(f"unregistered ModelCandidate keys: {unknown}")
        try:
            require_coding_route_candidate_keys(
                route_key,
                tuple(candidate_key),
                runtime.state.model_registry,
            )
        except InvariantViolation as exc:
            raise typer.BadParameter(str(exc)) from exc
        snapshot = RouteSnapshot(
            snapshot_id=snapshot_id,
            data_space_id=runtime.kernel.data_space.data_space_id,
            route_key=route_key,
            evidence_cursor=evidence_cursor,
            candidate_keys=tuple(candidate_key),
            production_objective=objective,
            state=RouteSnapshotState.DRAFT,
            s3_star_approval_event_id=None,
            owner_approval_event_id=None,
        )
        runtime.kernel.register_route_snapshot(
            snapshot,
            actor_id=owner_actor_id,
            idempotency_key=f"{idempotency_prefix}:register",
        )
        runtime.kernel.approve_route_snapshot(
            snapshot_id,
            approval="s3_star",
            actor_id=s3_star_actor_id,
            idempotency_key=f"{idempotency_prefix}:s3-star",
        )
        runtime.kernel.approve_route_snapshot(
            snapshot_id,
            approval="owner",
            actor_id=owner_actor_id,
            idempotency_key=f"{idempotency_prefix}:owner",
        )
        runtime.kernel.publish_route_snapshot(
            snapshot_id,
            actor_id=owner_actor_id,
            idempotency_key=f"{idempotency_prefix}:publish",
        )
        _json(runtime.kernel.route_snapshots[snapshot_id])
    finally:
        runtime.close()


@verification_app.command("commission")
def verification_commission(
    config: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Create the local Interface Node, conversation, and approved route once."""
    runtime = bootstrap(config, require_active_route=False)
    try:
        loaded = runtime.loaded.config
        if loaded.deployment.mode != "local_verification":
            raise typer.BadParameter(
                "verification commission requires deployment.mode=local_verification"
            )
        data_space = loaded.kernel.data_space
        interface_config = loaded.interface_pilot
        node = UVSMNode(
            node_id=interface_config.node_id,
            data_space_id=data_space.data_space_id,
            owner_id=data_space.owner_id,
            name="Local owner Interface Node",
            kind=NodeKind.INTERFACE,
            parent_node_id=None,
            resident_functions=frozenset(VSMFunction),
            resident_s3_parent_function=VSMFunction.S5,
            status=NodeStatus.ACTIVE,
            memory_stream_id="memory:local-interface",
        )
        stored_node = runtime.kernel.nodes.get(node.node_id)
        if stored_node is None:
            runtime.kernel.register_node(
                node,
                actor_id=data_space.owner_id,
                idempotency_key="local-verification:interface-node",
            )
        elif stored_node != node:
            raise InvariantViolation(
                "existing local Interface Node differs from commissioning contract"
            )

        conversation = Conversation(
            conversation_id="conversation:local-verification",
            data_space_id=data_space.data_space_id,
            interface_node_id=node.node_id,
            owner_id=data_space.owner_id,
            title="Local verification",
        )
        surface_binding = SurfaceBinding(
            binding_id="binding:local-verification",
            conversation_id=conversation.conversation_id,
            surface="discord",
            source_session_id="local-verification",
            channel_id="local-verification",
            device_id="device:owner-local",
        )
        stored_conversation = runtime.interface.conversations.get(
            conversation.conversation_id
        )
        if stored_conversation is None:
            runtime.interface.create_conversation(
                conversation,
                surface_binding,
                idempotency_key="local-verification:conversation",
            )
        else:
            if stored_conversation != conversation:
                raise InvariantViolation(
                    "existing local Conversation differs from commissioning contract"
                )

        registry = runtime.state.model_registry
        candidate = next(
            item
            for item in registry.values()
            if (
                item.adapter == interface_config.adapter
                and item.adapter_version == interface_config.adapter_version
                and item.provider == interface_config.provider
                and item.selection == interface_config.model_selection
                and item.model_snapshot == interface_config.model_snapshot
                and item.effort == interface_config.effort
                and item.toolset == interface_config.toolset
                and item.sandbox_fingerprint
                == interface_config.sandbox_fingerprint
                and item.environment_fingerprint
                == interface_config.environment_fingerprint
            )
        )
        snapshot_id = loaded.routing.active_route_snapshot_id
        snapshot = runtime.kernel.route_snapshots.get(snapshot_id)
        if snapshot is None:
            snapshot = RouteSnapshot(
                snapshot_id=snapshot_id,
                data_space_id=data_space.data_space_id,
                route_key="interface:local-verification",
                evidence_cursor=runtime.state.routing_evidence.evidence_cursor,
                candidate_keys=(candidate.key,),
                production_objective="quality_max",
                state=RouteSnapshotState.DRAFT,
                s3_star_approval_event_id=None,
                owner_approval_event_id=None,
            )
            runtime.kernel.register_route_snapshot(
                snapshot,
                actor_id=data_space.owner_id,
                idempotency_key="local-verification:route:register",
            )
            runtime.kernel.approve_route_snapshot(
                snapshot_id,
                approval="s3_star",
                actor_id="actor:local-s3-star",
                idempotency_key="local-verification:route:s3-star",
            )
            runtime.kernel.approve_route_snapshot(
                snapshot_id,
                approval="owner",
                actor_id=data_space.owner_id,
                idempotency_key="local-verification:route:owner",
            )
            runtime.kernel.publish_route_snapshot(
                snapshot_id,
                actor_id=data_space.owner_id,
                idempotency_key="local-verification:route:publish",
            )
            snapshot = runtime.kernel.route_snapshots[snapshot_id]
        elif (
            snapshot.state is not RouteSnapshotState.PUBLISHED
            or snapshot.candidate_keys != (candidate.key,)
            or snapshot.evidence_cursor
            != runtime.state.routing_evidence.evidence_cursor
        ):
            raise InvariantViolation(
                "existing local RouteSnapshot differs from commissioning contract"
            )
        _json(
            {
                "deployment_mode": loaded.deployment.mode,
                "data_space_id": data_space.data_space_id,
                "interface_node_id": node.node_id,
                "conversation_id": conversation.conversation_id,
                "route_snapshot_id": snapshot.snapshot_id,
                "route_state": snapshot.state,
                "candidate_key": candidate.key,
                "model_snapshot": candidate.model_snapshot,
                "effort": candidate.effort,
            }
        )
    finally:
        runtime.close()


@app.command()
def overview(
    config: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Show persistent Node, WorkItem, Execution, and conversation counts."""
    runtime = bootstrap(config)
    try:
        _json(
            {
                "data_space": runtime.kernel.data_space,
                "projection_cursor": runtime.projection.cursor,
                "projection_sha256": runtime.projection.digest(),
                "nodes": len(runtime.kernel.nodes),
                "work_items": len(runtime.kernel.work_items),
                "executions": len(runtime.kernel.executions),
                "conversations": len(runtime.interface.conversations),
                "open_commitments": sum(
                    item.state == "open"
                    for item in runtime.interface.commitments.values()
                ),
            }
        )
    finally:
        runtime.close()


@app.command("inspect")
def inspect_control_plane(
    resource: InspectResource = typer.Argument(...),
    base_url: str = typer.Option(..., min=1),
    bearer_token_env: str = typer.Option(..., min=1),
    device_id: str = typer.Option(..., min=1),
    after_cursor: int = typer.Option(0, min=0),
    limit: int = typer.Option(250, min=1, max=1000),
) -> None:
    """Read a live Interface Projection without invoking a Pilot model."""
    token = os.environ.get(bearer_token_env)
    if token is None or not token.strip():
        raise ConfigurationError(
            f"required environment variable is missing: {bearer_token_env}"
        )
    path = f"/api/{resource.value}"
    params = None
    if resource is InspectResource.EVENTS:
        params = {"after_cursor": after_cursor, "limit": limit}
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {token}",
            "X-Nanihold-Device-Id": device_id,
        },
        timeout=30.0,
    ) as client:
        response = client.get(path, params=params)
    if not response.is_success:
        raise InvariantViolation(
            f"Nanihold API HTTP {response.status_code}: {response.text}"
        )
    _json(response.json())


@app.command("tui")
def tui_dashboard(
    base_url: str = typer.Option(..., min=1),
    bearer_token_env: str = typer.Option(..., min=1),
    device_id: str = typer.Option(..., min=1),
    width: int = typer.Option(88, min=60, max=240),
) -> None:
    """Render live model-free owner projections in a terminal."""
    token = os.environ.get(bearer_token_env)
    if token is None or not token.strip():
        raise ConfigurationError(
            f"required environment variable is missing: {bearer_token_env}"
        )
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {token}",
            "X-Nanihold-Device-Id": device_id,
        },
        timeout=30.0,
    ) as client:
        snapshot = load_operational_snapshot(client)
    typer.echo(render_dashboard(snapshot, width=width), nl=False)


def _owner_api_client(
    *, base_url: str, bearer_token_env: str, device_id: str
) -> httpx.Client:
    token = os.environ.get(bearer_token_env)
    if token is None or not token.strip():
        raise ConfigurationError(
            f"required environment variable is missing: {bearer_token_env}"
        )
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={
            "Authorization": f"Bearer {token}",
            "X-Nanihold-Device-Id": device_id,
        },
        timeout=30.0,
    )


def _owner_id(client: httpx.Client) -> str:
    response = client.get("/api/data-spaces")
    if not response.is_success:
        raise InvariantViolation(
            f"Nanihold API HTTP {response.status_code}: {response.text}"
        )
    spaces = response.json()
    if (
        not isinstance(spaces, list)
        or len(spaces) != 1
        or not isinstance(spaces[0], dict)
        or not isinstance(spaces[0].get("owner_id"), str)
        or not spaces[0]["owner_id"]
    ):
        raise InvariantViolation("owner DataSpace projection is not exact")
    return spaces[0]["owner_id"]


def _compact_activation_status(document: object) -> dict[str, object]:
    if not isinstance(document, dict):
        raise InvariantViolation("activation status must be a JSON object")
    state = document.get("state")
    assessment = document.get("assessment")
    error = document.get("reorientation_error")
    if (
        not isinstance(state, str)
        or not state
        or (assessment is not None and not isinstance(assessment, dict))
        or (error is not None and not isinstance(error, str))
    ):
        raise InvariantViolation("activation status fields are invalid")
    return {
        "state": state,
        "assessment_ready": assessment is not None,
        "reorientation_error": error,
    }


@reorientation_app.command("start")
def reorientation_start(
    base_url: str = typer.Option(..., min=1),
    bearer_token_env: str = typer.Option(..., min=1),
    device_id: str = typer.Option(..., min=1),
    idempotency_key: str = typer.Option(..., min=1),
) -> None:
    """Start or explicitly retry the bounded history reorientation."""
    with _owner_api_client(
        base_url=base_url,
        bearer_token_env=bearer_token_env,
        device_id=device_id,
    ) as client:
        response = client.post(
            "/api/reorientation/start",
            json={
                "actor_id": _owner_id(client),
                "idempotency_key": idempotency_key,
            },
        )
    if not response.is_success:
        raise InvariantViolation(
            f"Nanihold API HTTP {response.status_code}: {response.text}"
        )
    _json(_compact_activation_status(response.json()))


@reorientation_app.command("approve")
def reorientation_approve(
    base_url: str = typer.Option(..., min=1),
    bearer_token_env: str = typer.Option(..., min=1),
    device_id: str = typer.Option(..., min=1),
    idempotency_key: str = typer.Option(..., min=1),
    correction: list[str] | None = typer.Option(None, "--correction"),
) -> None:
    """Confirm the current assessment without asking for internal IDs."""
    with _owner_api_client(
        base_url=base_url,
        bearer_token_env=bearer_token_env,
        device_id=device_id,
    ) as client:
        status_response = client.get("/api/activation/status")
        if not status_response.is_success:
            raise InvariantViolation(
                f"Nanihold API HTTP {status_response.status_code}: "
                f"{status_response.text}"
            )
        status_document = status_response.json()
        if not isinstance(status_document, dict):
            raise InvariantViolation("activation status must be a JSON object")
        assessment = status_document.get("assessment")
        if (
            status_document.get("state") != "AWAITING_OWNER_CONFIRMATION"
            or not isinstance(assessment, dict)
            or not isinstance(assessment.get("assessment_id"), str)
            or not assessment["assessment_id"]
            or not isinstance(assessment.get("conversation_id"), str)
            or not assessment["conversation_id"]
        ):
            raise InvariantViolation(
                "Interface reorientation has no owner-confirmable assessment"
            )
        resume_work_item_ids = assessment.get("resume_work_item_ids")
        if (
            not isinstance(resume_work_item_ids, list)
            or not resume_work_item_ids
            or any(
                not isinstance(work_item_id, str) or not work_item_id
                for work_item_id in resume_work_item_ids
            )
        ):
            raise InvariantViolation(
                "Interface assessment has no real resume WorkItem; "
                "request reorientation revision instead of approval"
            )
        response = client.post(
            "/api/reorientation/approval",
            json={
                "assessment_id": assessment["assessment_id"],
                "conversation_id": assessment["conversation_id"],
                "corrections": correction or [],
                "actor_id": _owner_id(client),
                "idempotency_key": idempotency_key,
            },
        )
    if not response.is_success:
        raise InvariantViolation(
            f"Nanihold API HTTP {response.status_code}: {response.text}"
        )
    _json(_compact_activation_status(response.json()))


@reorientation_app.command("revise")
def reorientation_revise(
    base_url: str = typer.Option(..., min=1),
    bearer_token_env: str = typer.Option(..., min=1),
    device_id: str = typer.Option(..., min=1),
    idempotency_key: str = typer.Option(..., min=1),
    reason: ReorientationRevisionReason = typer.Option(...),
) -> None:
    """Return an incomplete assessment to reorientation-only state."""
    with _owner_api_client(
        base_url=base_url,
        bearer_token_env=bearer_token_env,
        device_id=device_id,
    ) as client:
        response = client.post(
            "/api/reorientation/revision",
            json={
                "reason_code": reason.value,
                "requested_by": "owner",
                "actor_id": _owner_id(client),
                "idempotency_key": idempotency_key,
            },
        )
    if not response.is_success:
        raise InvariantViolation(
            f"Nanihold API HTTP {response.status_code}: {response.text}"
        )
    _json(_compact_activation_status(response.json()))


@events_app.command("tail")
def events_tail(
    config: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
    after_cursor: int = typer.Option(..., min=0),
    limit: int = typer.Option(..., min=1, max=1000),
) -> None:
    """Read an explicit cursor page without invoking any model."""
    runtime = bootstrap(config)
    try:
        events = runtime.kernel.ledger.page(after_cursor, limit)
        _json(
            {
                "events": [item.model_dump(mode="json") for item in events],
                "next_cursor": events[-1].cursor if events else after_cursor,
            }
        )
    finally:
        runtime.close()


@app.command("trace")
def execution_trace(
    execution_id: str = typer.Argument(..., min=1),
    config: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
) -> None:
    """Show one Execution's dispatch, receipt, and provider session timeline."""
    runtime = bootstrap(config)
    try:
        _json(_execution_trace(runtime, execution_id))
    finally:
        runtime.close()


@migration_app.command("scan")
def migration_scan(
    source: Path = typer.Option(..., exists=True, file_okay=False, readable=True),
    summary: bool = typer.Option(
        False, help="Print counts and digests without the per-file manifest."
    ),
) -> None:
    """Inventory the legacy archive and list every ownership decision required."""
    census, _ = scan_legacy(source)
    if summary:
        _json(
            {
                "source_root": census.source_root,
                "file_count": census.file_count,
                "byte_count": census.byte_count,
                "event_log_count": census.event_log_count,
                "relevant_record_count": census.relevant_record_count,
                "relevant_counts": census.relevant_counts,
                "required_source_assignments": census.required_source_assignments,
                "manifest_sha256": census.manifest_sha256,
                "relevant_sha256": census.relevant_sha256,
            }
        )
        return
    _json(census)


@migration_app.command("dry-run")
def migration_dry_run(
    source: Path = typer.Option(..., exists=True, file_okay=False, readable=True),
    assignment: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
    output: Path = typer.Option(..., dir_okay=False),
) -> None:
    """Freeze counts and hashes after exact ownership assignment."""
    if output.exists():
        raise typer.BadParameter(f"output already exists: {output}")
    plan = build_plan(source, assignment)
    output.write_text(plan.model_dump_json(indent=2), "utf-8")
    _json(plan)


@migration_app.command("import")
def migration_import(
    config: Path = typer.Option(..., exists=True, dir_okay=False, readable=True),
    source: Path = typer.Option(..., exists=True, file_okay=False, readable=True),
    plan_path: Path = typer.Option(
        ..., "--plan", exists=True, dir_okay=False, readable=True
    ),
    receipt: Path = typer.Option(..., dir_okay=False),
) -> None:
    """Import the frozen plan into LETHE and prove dry-run/import equality."""
    if receipt.exists():
        raise typer.BadParameter(f"receipt already exists: {receipt}")
    plan = MigrationPlan.model_validate_json(plan_path.read_text("utf-8"))
    runtime = bootstrap(config)
    try:
        missing_nodes = sorted(
            {
                assignment.node_id
                for assignment in plan.assignment.sources.values()
                if assignment.node_id not in runtime.kernel.nodes
            }
            | {
                assignment.interface_node_id
                for assignment in plan.assignment.sources.values()
                if assignment.interface_node_id not in runtime.kernel.nodes
            }
        )
        if missing_nodes:
            raise typer.BadParameter(
                f"ownership assignment references missing Nodes: {missing_nodes}"
            )
        result = import_plan(
            plan,
            source_root=source,
            ledger=runtime.ledger,
            data_space_id=runtime.kernel.data_space.data_space_id,
        )
        receipt.write_text(result.model_dump_json(indent=2), "utf-8")
        _json(result)
    finally:
        runtime.close()


@migration_app.command("archive")
def migration_archive(
    source: Path = typer.Option(..., exists=True, file_okay=False, readable=True),
    destination: Path = typer.Option(..., file_okay=False),
    plan_path: Path = typer.Option(
        ..., "--plan", exists=True, dir_okay=False, readable=True
    ),
) -> None:
    """Copy every legacy byte into a digest-verified read-only archive."""
    plan = MigrationPlan.model_validate_json(plan_path.read_text("utf-8"))
    census = archive_legacy(
        source, destination, plan.census.manifest_sha256
    )
    _json(census)
