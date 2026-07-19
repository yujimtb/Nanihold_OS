from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path

import httpx
import typer

from vsm.errors import ConfigurationError, InvariantViolation
from vsm.config import load_config
from vsm.migration.legacy import (
    MigrationPlan,
    archive_legacy,
    build_plan,
    import_plan,
    scan_legacy,
)
from vsm.runtime import bootstrap, build_app
from vsm.kernel.models import RouteSnapshot, RouteSnapshotState

app = typer.Typer(
    name="vsm",
    help="Nanihold Node/Work/Event control plane.",
    no_args_is_help=True,
)
events_app = typer.Typer(help="Read the canonical Event Ledger.")
migration_app = typer.Typer(help="One-time legacy archive migration.")
routes_app = typer.Typer(help="Commission and inspect approved Bayesian routes.")
app.add_typer(events_app, name="events")
app.add_typer(migration_app, name="migration")
app.add_typer(routes_app, name="routes")


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
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    ) as client:
        response = client.get(path, params=params)
    if not response.is_success:
        raise InvariantViolation(
            f"Nanihold API HTTP {response.status_code}: {response.text}"
        )
    _json(response.json())


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
