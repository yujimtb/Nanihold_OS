from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from vsm.errors import InvariantViolation, NaniholdError
from vsm.interface.models import Conversation
from vsm.interface.service import InterfaceService
from vsm.kernel.models import Execution, RouteSnapshot, UVSMNode, WorkItem
from vsm.kernel.service import Kernel
from vsm.pilot.host import PilotHostCoordinator
from vsm.pilot.models import (
    DeviceIdentity,
    ModelCandidate,
    PilotHostState,
)
from vsm.routing.bayesian import (
    BayesianRouter,
    RoutingEvidenceService,
    VerifiedRouteOutcome,
)
from vsm.token_lab.lab import (
    TokenBaseline,
    TokenEfficiencyLab,
    TokenLabEventService,
    TokenObservation,
)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandMetadata(StrictRequest):
    actor_id: str
    idempotency_key: str = Field(min_length=1)


class CreateNodeRequest(CommandMetadata):
    node: UVSMNode


class CreateWorkItemRequest(CommandMetadata):
    work_item: WorkItem


class CreateExecutionRequest(CommandMetadata):
    execution: Execution


class WorkInterventionRequest(CommandMetadata):
    reason: str = Field(min_length=1)


class CreateConversationRequest(StrictRequest):
    conversation: Conversation
    idempotency_key: str = Field(min_length=1)


class OwnerMessageRequest(StrictRequest):
    text: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    force_new_pilot: bool


class PilotHostConnectRequest(StrictRequest):
    identity: DeviceIdentity
    acknowledged_cursor: int = Field(ge=0)
    connected_at: datetime


class PilotHostDisconnectRequest(StrictRequest):
    disconnected_at: datetime
    idempotency_key: str = Field(min_length=1)


class RouteApprovalRequest(CommandMetadata):
    approval: str = Field(pattern=r"^(s3_star|owner)$")


class RegisterRouteSnapshotRequest(CommandMetadata):
    route_snapshot: RouteSnapshot


class VerifiedRouteOutcomeRequest(CommandMetadata):
    outcome: VerifiedRouteOutcome


class TokenObservationRequest(CommandMetadata):
    observation: TokenObservation


class TokenBaselineRequest(CommandMetadata):
    baseline: TokenBaseline


class TokenWeeklyReviewRequest(CommandMetadata):
    reviewed_at: datetime


@dataclass
class AppState:
    kernel: Kernel
    interface: InterfaceService
    pilot_hosts: PilotHostCoordinator
    router: BayesianRouter
    routing_evidence: RoutingEvidenceService
    token_lab: TokenEfficiencyLab
    token_lab_events: TokenLabEventService
    model_registry: dict[str, ModelCandidate]
    api_bearer_token: str


def create_app(state: AppState, *, allowed_origins: tuple[str, ...]) -> FastAPI:
    if not allowed_origins:
        raise InvariantViolation("Interface allowed_origins must be explicit")
    app = FastAPI(title="Nanihold OS", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if authorization != f"Bearer {state.api_bearer_token}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="valid Bearer token required",
            )

    @app.exception_handler(NaniholdError)
    async def nanihold_error_handler(_request: Any, exc: NaniholdError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.get("/api/data-spaces", dependencies=[Depends(authorize)])
    def data_spaces():
        return [state.kernel.data_space]

    @app.get("/api/nodes", dependencies=[Depends(authorize)])
    def nodes():
        return {
            "items": list(state.kernel.nodes.values()),
            "capability_grants": list(state.kernel.capability_grants.values()),
            "reference_grants": list(state.kernel.reference_grants.values()),
        }

    @app.post("/api/nodes", dependencies=[Depends(authorize)], status_code=201)
    def create_node(request: CreateNodeRequest):
        state.kernel.register_node(
            request.node,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return request.node

    @app.get("/api/work-items", dependencies=[Depends(authorize)])
    def work_items():
        return {
            "items": list(state.kernel.work_items.values()),
            "edges": state.kernel.work_edges,
        }

    @app.post("/api/work-items", dependencies=[Depends(authorize)], status_code=201)
    def create_work_item(request: CreateWorkItemRequest):
        state.kernel.create_work_item(
            request.work_item,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return request.work_item

    @app.post(
        "/api/work-items/{work_item_id}/interventions",
        dependencies=[Depends(authorize)],
    )
    def intervene_work_item(
        work_item_id: str, request: WorkInterventionRequest
    ):
        state.kernel.intervene(
            work_item_id,
            actor_id=request.actor_id,
            reason=request.reason,
            idempotency_key=request.idempotency_key,
        )
        return state.kernel.work_items[work_item_id]

    @app.get("/api/executions", dependencies=[Depends(authorize)])
    def executions():
        return {
            "items": list(state.kernel.executions.values()),
            "effect_leases": list(state.kernel.effect_leases.values()),
            "budget_reservations": list(state.kernel.budget_reservations.values()),
        }

    @app.post("/api/executions", dependencies=[Depends(authorize)], status_code=201)
    def create_execution(request: CreateExecutionRequest):
        state.kernel.create_execution(
            request.execution,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return request.execution

    @app.get("/api/events", dependencies=[Depends(authorize)])
    def events(
        after_cursor: Annotated[int, Query(ge=0)],
        limit: Annotated[int, Query(gt=0, le=1000)],
    ):
        items = state.kernel.ledger.page(after_cursor, limit)
        return {
            "events": items,
            "next_cursor": items[-1].cursor if items else after_cursor,
        }

    @app.get("/api/conversations", dependencies=[Depends(authorize)])
    def conversations():
        visible_messages = {}
        for conversation_id, items in state.interface.messages.items():
            visible = []
            for item in items:
                payload = item.model_dump(mode="json")
                if item.role == "owner":
                    if item.blob_ref is None:
                        raise InvariantViolation(
                            "owner message is missing its LETHE BlobRef"
                        )
                    try:
                        payload["display_text"] = state.kernel.ledger.get_blob(
                            item.blob_ref
                        ).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise InvariantViolation(
                            "owner message blob is not UTF-8"
                        ) from exc
                visible.append(payload)
            visible_messages[conversation_id] = visible
        return {
            "items": list(state.interface.conversations.values()),
            "messages": visible_messages,
            "commitments": list(state.interface.commitments.values()),
            "decisions": list(state.interface.decisions.values()),
            "node_memories": list(state.interface.node_memories.values()),
        }

    @app.post("/api/conversations", dependencies=[Depends(authorize)], status_code=201)
    def create_conversation(request: CreateConversationRequest):
        return state.interface.create_conversation(
            request.conversation, idempotency_key=request.idempotency_key
        )

    @app.get(
        "/api/conversations/{conversation_id}", dependencies=[Depends(authorize)]
    )
    def conversation_status(conversation_id: str):
        return state.interface.status(conversation_id)

    @app.post(
        "/api/conversations/{conversation_id}/messages",
        dependencies=[Depends(authorize)],
    )
    def owner_message(conversation_id: str, request: OwnerMessageRequest):
        return state.interface.turn(
            conversation_id=conversation_id,
            owner_text=request.text,
            idempotency_key=request.idempotency_key,
            force_new_pilot=request.force_new_pilot,
        )

    @app.get("/api/pilot-hosts", dependencies=[Depends(authorize)])
    def pilot_hosts():
        return list(state.pilot_hosts.hosts.values())

    @app.post(
        "/api/pilot-hosts/connect", dependencies=[Depends(authorize)], status_code=201
    )
    def connect_pilot_host(request: PilotHostConnectRequest):
        return state.pilot_hosts.connect(
            identity=request.identity,
            acknowledged_cursor=request.acknowledged_cursor,
            connected_at=request.connected_at,
        )

    @app.post(
        "/api/pilot-hosts/{pilot_host_id}/disconnect",
        dependencies=[Depends(authorize)],
    )
    def disconnect_pilot_host(
        pilot_host_id: str, request: PilotHostDisconnectRequest
    ):
        return state.pilot_hosts.disconnect(
            pilot_host_id,
            disconnected_at=request.disconnected_at,
            idempotency_key=request.idempotency_key,
        )

    @app.get("/api/model-registry", dependencies=[Depends(authorize)])
    def model_registry():
        return {
            "candidates": list(state.model_registry.values()),
            "verified_outcomes": list(state.routing_evidence.outcomes.values()),
            "evidence_cursor": state.routing_evidence.evidence_cursor,
        }

    @app.post(
        "/api/model-registry/outcomes",
        dependencies=[Depends(authorize)],
        status_code=201,
    )
    def record_model_outcome(request: VerifiedRouteOutcomeRequest):
        state.routing_evidence.record(
            request.outcome,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return request.outcome

    @app.get("/api/route-snapshots", dependencies=[Depends(authorize)])
    def route_snapshots():
        snapshots = list(state.kernel.route_snapshots.values())
        return {
            "items": snapshots,
            "scores": {
                snapshot.snapshot_id: state.router.scores(snapshot.candidate_keys)
                for snapshot in snapshots
            },
        }

    @app.post(
        "/api/route-snapshots", dependencies=[Depends(authorize)], status_code=201
    )
    def register_route_snapshot(request: RegisterRouteSnapshotRequest):
        snapshot = request.route_snapshot
        if snapshot.evidence_cursor != state.routing_evidence.evidence_cursor:
            raise InvariantViolation(
                "RouteSnapshot evidence_cursor must equal current verified evidence cursor"
            )
        unknown = set(snapshot.candidate_keys) - set(state.model_registry)
        if unknown:
            raise InvariantViolation(
                f"RouteSnapshot references unregistered ModelCandidates: {sorted(unknown)}"
            )
        state.kernel.register_route_snapshot(
            snapshot,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return snapshot

    @app.post(
        "/api/route-snapshots/{snapshot_id}/approvals",
        dependencies=[Depends(authorize)],
    )
    def approve_route_snapshot(snapshot_id: str, request: RouteApprovalRequest):
        state.kernel.approve_route_snapshot(
            snapshot_id,
            approval=request.approval,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return state.kernel.route_snapshots[snapshot_id]

    @app.post(
        "/api/route-snapshots/{snapshot_id}/publish",
        dependencies=[Depends(authorize)],
    )
    def publish_route_snapshot(snapshot_id: str, request: CommandMetadata):
        state.kernel.publish_route_snapshot(
            snapshot_id,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return state.kernel.route_snapshots[snapshot_id]

    @app.get("/api/token-lab", dependencies=[Depends(authorize)])
    def token_lab():
        return {
            "baselines": list(state.token_lab.baselines.values()),
            "observations": state.token_lab.observations,
            "last_weekly_review_at": state.token_lab.last_weekly_review_at,
            "weekly_due": state.token_lab.weekly_due(datetime.now(UTC)),
        }

    @app.post(
        "/api/token-lab/baselines",
        dependencies=[Depends(authorize)],
        status_code=201,
    )
    def approve_token_baseline(request: TokenBaselineRequest):
        state.token_lab_events.approve_baseline(
            request.baseline,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return request.baseline

    @app.post(
        "/api/token-lab/observations",
        dependencies=[Depends(authorize)],
        status_code=201,
    )
    def observe_tokens(request: TokenObservationRequest):
        _, triggers = state.token_lab_events.observe(
            request.observation,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return {"triggers": triggers}

    @app.post(
        "/api/token-lab/weekly-reviews",
        dependencies=[Depends(authorize)],
        status_code=201,
    )
    def record_token_weekly_review(request: TokenWeeklyReviewRequest):
        state.token_lab_events.record_weekly_review(
            request.reviewed_at,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return {"reviewed_at": request.reviewed_at}

    @app.websocket("/api/pilot-hosts/{pilot_host_id}/stream")
    async def pilot_stream(websocket: WebSocket, pilot_host_id: str):
        if websocket.headers.get("authorization") != f"Bearer {state.api_bearer_token}":
            await websocket.close(code=4401)
            return
        await websocket.accept()
        connected = False
        cursor = 0
        try:
            hello = await websocket.receive_json()
            identity = DeviceIdentity.model_validate(hello["identity"])
            if identity.pilot_host_id != pilot_host_id:
                raise InvariantViolation("PilotHost identity mismatch")
            cursor = int(hello["acknowledged_cursor"])
            state.pilot_hosts.connect(
                identity=identity,
                acknowledged_cursor=cursor,
                connected_at=datetime.fromisoformat(hello["connected_at"]),
            )
            connected = True
            while True:
                ledger_events = state.kernel.ledger.page(cursor, 100)
                await websocket.send_json(
                    {
                        "events": [
                            item.model_dump(mode="json") for item in ledger_events
                        ],
                        "cursor": ledger_events[-1].cursor if ledger_events else cursor,
                    }
                )
                message = await websocket.receive_json()
                if message.get("kind") not in ("ack", "tail"):
                    raise InvariantViolation(
                        "PilotHost stream accepts only ack or tail"
                    )
                if message.get("kind") == "ack":
                    cursor = int(message["cursor"])
                    state.pilot_hosts.acknowledge(pilot_host_id, cursor)
        except WebSocketDisconnect:
            pass
        finally:
            if connected:
                host = state.pilot_hosts.hosts.get(pilot_host_id)
                if host is not None and host.state is PilotHostState.CONNECTED:
                    state.pilot_hosts.disconnect(
                        pilot_host_id,
                        disconnected_at=datetime.now(UTC),
                        idempotency_key=f"pilot-disconnect:{pilot_host_id}:{cursor}",
                    )

    return app
