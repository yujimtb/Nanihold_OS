from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import (
    BackgroundTasks,
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vsm.errors import InvariantViolation, NaniholdError
from vsm.activation.models import (
    CurrentWorkGraphSnapshot,
    HistoryImportReceipt,
    ReorientationAssessment,
    ReorientationRevisionReason,
)
from vsm.activation.reorientation import (
    HistoryReader,
    HistoryToolService,
    ReorientationService,
)
from vsm.dispatcher import DependencyAwareDispatcher, PilotBinding
from vsm.interface.models import (
    Conversation,
    OwnerMessageAction,
    ReadHistoryAction,
    SurfaceBinding,
)
from vsm.interface.service import InterfaceService
from vsm.ids import new_id
from vsm.kernel.models import (
    Execution,
    RouteSnapshot,
    RouteSnapshotRetirementReason,
    RouteSnapshotState,
    UVSMNode,
    WorkItem,
)
from vsm.kernel.service import Kernel
from vsm.notifications import AgentNotification
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
    require_coding_route_candidate_keys,
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


class DeliverNotificationRequest(CommandMetadata):
    notification: AgentNotification


class PromoteNotificationRequest(CommandMetadata):
    work_item: WorkItem


class WorkInterventionRequest(CommandMetadata):
    reason: str = Field(min_length=1)


class CreateConversationRequest(StrictRequest):
    conversation: Conversation
    surface_binding: SurfaceBinding
    idempotency_key: str = Field(min_length=1)


class HistoryImportRequest(CommandMetadata):
    work_graph_snapshot: CurrentWorkGraphSnapshot
    receipt: HistoryImportReceipt
    reorientation_conversation_id: str = Field(min_length=1)


class ReorientationAssessmentRequest(CommandMetadata):
    assessment: ReorientationAssessment


class ReorientationRevisionRequest(CommandMetadata):
    reason_code: ReorientationRevisionReason
    requested_by: Literal["owner", "system"]


class HistoryQueryRequest(CommandMetadata):
    action: ReadHistoryAction


class ReorientationApprovalRequest(CommandMetadata):
    assessment_id: str
    conversation_id: str
    corrections: tuple[Annotated[str, Field(min_length=1)], ...]


class WorkDelegationRequest(CommandMetadata):
    delegated_to_node_id: str


class DispatchWorkItemRequest(CommandMetadata):
    pass


class EffectApprovalRequest(CommandMetadata):
    pass


class OwnerBootstrapExchangeRequest(StrictRequest):
    code: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


class OwnerBootstrapIssueRequest(StrictRequest):
    base_url: str = Field(min_length=1)
    lifetime_seconds: Annotated[int, Field(ge=1, le=900)]
    idempotency_key: str = Field(min_length=1)


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


class RetireRouteSnapshotRequest(CommandMetadata):
    reason_code: RouteSnapshotRetirementReason
    replacement_snapshot_id: Annotated[str, Field(min_length=1)] | None

    @model_validator(mode="after")
    def replacement_matches_reason(self) -> "RetireRouteSnapshotRequest":
        if (
            self.reason_code
            is RouteSnapshotRetirementReason.SUPERSEDED_BY_APPROVED_SNAPSHOT
            and self.replacement_snapshot_id is None
        ):
            raise ValueError("superseded RouteSnapshot requires a replacement")
        if (
            self.reason_code is RouteSnapshotRetirementReason.ROUTE_DECOMMISSIONED
            and self.replacement_snapshot_id is not None
        ):
            raise ValueError("decommissioned route forbids a replacement")
        return self


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
    authorized_device_ids: frozenset[str]
    dispatcher: DependencyAwareDispatcher
    owner_session_lifetime_seconds: int
    history_reader: HistoryReader
    history_max_result_bytes: int
    reorientation_service: ReorientationService | None = None
    reorientation_max_tool_rounds: int | None = None
    coding_pilot_id: str | None = None
    # Explicit owner opt-out (localhost-only deployments). Default False keeps
    # authentication enabled; when True the authorize dependency short-circuits
    # to a fixed device identity so no Bearer/device-id/session is required.
    owner_auth_disabled: bool = False


# Fixed device identity returned by authorize() when owner_auth_disabled is True.
OWNER_LOCAL_DEVICE_ID = "device:owner-local"


def _reorientation_failure_code(exc: Exception) -> str:
    """Return a bounded, non-sensitive activation error suitable for projections."""
    error_type = type(exc).__name__
    message = str(exc).replace("\r", "").replace("\n", "")
    candidate = f"{error_type}: {message}" if message else error_type
    if isinstance(exc, NaniholdError) and len(candidate.encode("utf-8")) <= 512:
        return candidate
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    return f"{error_type}: sha256:{digest}"


def create_app(state: AppState, *, allowed_origins: tuple[str, ...]) -> FastAPI:
    if not allowed_origins:
        raise InvariantViolation("Interface allowed_origins must be explicit")
    app = FastAPI(title="Nanihold OS", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Nanihold-Device-Id",
        ],
    )

    if not state.authorized_device_ids:
        raise InvariantViolation("at least one API device identity must be configured")

    @app.get("/health/live")
    def health_live():
        return {"status": "live", "model_calls": 0}

    @app.get("/health/ready")
    def health_ready():
        state.kernel.ledger.page(0, 1)
        return {
            "status": "ready",
            "activation_state": state.kernel.activation.state,
            "model_calls": 0,
        }

    def authorize(
        authorization: Annotated[str | None, Header()] = None,
        x_nanihold_device_id: Annotated[
        str | None, Header(alias="X-Nanihold-Device-Id")
        ] = None,
        owner_session: Annotated[
            str | None, Cookie(alias="nanihold_owner_session")
        ] = None,
    ) -> str:
        if state.owner_auth_disabled:
            # Explicit owner opt-out: bypass Bearer/device-id and owner session
            # checks entirely and return a fixed local device identity. Restore
            # authentication by setting server.owner_auth_disabled back to False.
            return OWNER_LOCAL_DEVICE_ID
        if owner_session is not None:
            try:
                return state.kernel.owner_bootstrap.authenticate(owner_session)
            except InvariantViolation as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=str(exc),
                ) from exc
        if authorization != f"Bearer {state.api_bearer_token}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="valid Bearer token required",
            )
        if x_nanihold_device_id not in state.authorized_device_ids:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="registered X-Nanihold-Device-Id required",
            )
        return x_nanihold_device_id

    @app.post("/api/owner-bootstrap/exchange")
    def exchange_owner_bootstrap(request: OwnerBootstrapExchangeRequest):
        grant = state.kernel.owner_bootstrap.exchange(
            code=request.code,
            device_id=request.device_id,
            session_lifetime_seconds=state.owner_session_lifetime_seconds,
            idempotency_key=request.idempotency_key,
        )
        response = JSONResponse(
            {
                "device_id": grant.device_id,
                "expires_at": grant.expires_at.isoformat(),
            }
        )
        response.set_cookie(
            "nanihold_owner_session",
            grant.session_token,
            httponly=True,
            secure=True,
            samesite="strict",
            expires=grant.expires_at,
            path="/",
        )
        return response

    @app.post(
        "/api/owner-bootstrap/issues",
        dependencies=[Depends(authorize)],
        status_code=201,
    )
    def issue_owner_bootstrap(request: OwnerBootstrapIssueRequest):
        base_url = request.base_url.rstrip("/")
        if base_url not in allowed_origins:
            raise InvariantViolation(
                "owner bootstrap base_url must be an allowed Interface origin"
            )
        return state.kernel.owner_bootstrap.issue(
            base_url=base_url,
            lifetime_seconds=request.lifetime_seconds,
            idempotency_key=request.idempotency_key,
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

    @app.get("/api/notifications", dependencies=[Depends(authorize)])
    def notifications():
        return {"items": list(state.kernel.agent_notifications.values())}

    @app.post("/api/notifications", dependencies=[Depends(authorize)], status_code=201)
    def deliver_notification(request: DeliverNotificationRequest):
        state.kernel.record_agent_notification(
            request.notification,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return request.notification

    @app.post(
        "/api/notifications/{notification_id}/promotions",
        dependencies=[Depends(authorize)],
        status_code=201,
    )
    def promote_notification(
        notification_id: str,
        request: PromoteNotificationRequest,
    ):
        state.kernel.promote_agent_notification(
            notification_id,
            request.work_item,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return state.kernel.work_items[request.work_item.work_item_id]

    @app.post(
        "/api/work-items/{work_item_id}/delegations",
        dependencies=[Depends(authorize)],
    )
    def delegate_work_item(work_item_id: str, request: WorkDelegationRequest):
        state.kernel.delegate_work_item(
            work_item_id,
            delegated_to_node_id=request.delegated_to_node_id,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return state.kernel.work_items[work_item_id]

    @app.post(
        "/api/work-items/{work_item_id}/dispatches",
        dependencies=[Depends(authorize)],
        status_code=202,
    )
    def dispatch_work_item(
        work_item_id: str,
        request: DispatchWorkItemRequest,
    ):
        work_item = state.kernel.work_items.get(work_item_id)
        if work_item is None:
            raise InvariantViolation("WorkItem not found")
        if state.coding_pilot_id is None:
            raise InvariantViolation("production coding Pilot is not configured")
        bindings = (
            PilotBinding(
                node_id=work_item.delegated_to_node_id,
                pilot_id=state.coding_pilot_id,
                pilot_host_id=state.pilot_hosts.expected_identity.pilot_host_id,
            ),
        )
        allowed_work_item_ids = frozenset({work_item_id})
        state.dispatcher.preflight_ready(
            bindings,
            allowed_work_item_ids=allowed_work_item_ids,
        )
        batch = state.dispatcher.dispatch_ready(
            bindings,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
            allowed_work_item_ids=allowed_work_item_ids,
        )
        if len(batch.assignments) != 1:
            raise InvariantViolation(
                "WorkItem dispatch did not create exactly one Execution"
            )
        return batch

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

    @app.post(
        "/api/effects/{lease_id}/approval",
        dependencies=[Depends(authorize)],
    )
    def approve_effect(lease_id: str, request: EffectApprovalRequest):
        state.kernel.approve_effect(
            lease_id,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return state.kernel.effect_approvals[lease_id]

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
            "surface_bindings": list(state.interface.surface_bindings.values()),
            "pilot_sessions": list(state.interface.pilot_sessions.values()),
            "messages": visible_messages,
            "commitments": list(state.interface.commitments.values()),
            "decisions": list(state.interface.decisions.values()),
            "node_memories": list(state.interface.node_memories.values()),
        }

    @app.post("/api/conversations", dependencies=[Depends(authorize)], status_code=201)
    def create_conversation(request: CreateConversationRequest):
        return state.interface.create_conversation(
            request.conversation,
            request.surface_binding,
            idempotency_key=request.idempotency_key,
        )

    @app.get(
        "/api/conversations/{conversation_id}", dependencies=[Depends(authorize)]
    )
    def conversation_status(conversation_id: str):
        return state.interface.status(conversation_id)

    @app.post("/api/conversations/{conversation_id}/actions", status_code=202)
    def owner_action(
        conversation_id: str,
        request: OwnerMessageAction,
        device_id: str = Depends(authorize),
    ):
        return state.interface.perform_owner_action(
            conversation_id=conversation_id,
            action=request,
            device_id=device_id,
        )

    @app.get("/api/conversations/{conversation_id}/actions/{action_id}")
    def owner_action_receipt(
        conversation_id: str,
        action_id: str,
        _device_id: str = Depends(authorize),
    ):
        return state.interface.action_receipt(conversation_id, action_id)

    @app.get("/api/history/imports", dependencies=[Depends(authorize)])
    def history_import():
        return {"receipt": state.kernel.activation.import_receipt}

    @app.post(
        "/api/history/imports", dependencies=[Depends(authorize)], status_code=201
    )
    def register_history_import(request: HistoryImportRequest):
        conversation = state.interface.conversations.get(
            request.reorientation_conversation_id
        )
        if conversation is None:
            raise InvariantViolation(
                "history import requires an existing canonical Conversation"
            )
        if conversation.data_space_id != state.kernel.data_space.data_space_id:
            raise InvariantViolation("canonical Conversation DataSpace mismatch")
        if conversation.owner_id != state.kernel.data_space.owner_id:
            raise InvariantViolation("canonical Conversation owner mismatch")
        state.kernel.import_current_work_graph(
            request.work_graph_snapshot,
            actor_id=request.actor_id,
            idempotency_key=f"{request.idempotency_key}:work-graph",
        )
        state.kernel.activation.register_history_import(
            request.receipt,
            reorientation_conversation_id=request.reorientation_conversation_id,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return state.kernel.activation.status()

    @app.get("/api/history/sessions", dependencies=[Depends(authorize)])
    def history_sessions():
        return {
            "items": [
                {
                    "session_ref": session.session_ref,
                    "source_session_id": session.source_session_id,
                    "source_kind": session.source_kind,
                    "source_id": session.source_id,
                    "message_count": session.message_count,
                    "first_message_at": session.first_message_at,
                    "last_message_at": session.last_message_at,
                }
                for session in state.kernel.activation.sessions.values()
            ],
            "model_calls": 0,
        }

    @app.get("/api/reorientation", dependencies=[Depends(authorize)])
    def reorientation():
        return {
            "state": state.kernel.activation.state,
            "assessment": state.kernel.activation.assessment,
            "model_calls": 0,
        }

    @app.post(
        "/api/reorientation/start",
        dependencies=[Depends(authorize)],
        status_code=202,
    )
    def start_reorientation(
        request: CommandMetadata, background_tasks: BackgroundTasks
    ):
        if (
            state.reorientation_service is None
            or state.reorientation_max_tool_rounds is None
        ):
            raise InvariantViolation(
                "production reorientation service is not configured"
            )
        receipt = state.kernel.activation.import_receipt
        if receipt is None:
            raise InvariantViolation("verified history handoff is missing")
        state.kernel.activation.start_reorientation(
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        initial_action = ReadHistoryAction(
            action_id=new_id("action"),
            kind="history.read",
            operation="get_current_state",
            argument=None,
            page_cursor=None,
        )

        def execute_reorientation() -> None:
            try:
                state.reorientation_service.execute(
                    initial_action=initial_action,
                    actor_id=request.actor_id,
                    idempotency_key=f"{request.idempotency_key}:execute",
                    max_tool_rounds=state.reorientation_max_tool_rounds,
                    objective=(
                        "Review the complete indexed history, resolve uncertainty "
                        "with bounded LETHE queries, and submit ReorientationAssessment. "
                        "Treat historical interface or model display names as "
                        "non-authoritative: the persistent subject is the owner "
                        "Interface Node and canonical Conversation. Do not assign or "
                        "assume a personal or role name for the unnamed Interface Pilot."
                    ),
                    session_index_ref=receipt.session_index_ref,
                    open_commitment_refs=(receipt.open_commitments_ref,),
                    current_state_ref=receipt.current_state_ref,
                )
            except Exception as exc:
                state.kernel.activation.record_reorientation_failure(
                    error_code=_reorientation_failure_code(exc),
                    actor_id=request.actor_id,
                    idempotency_key=f"{request.idempotency_key}:failure",
                )

        background_tasks.add_task(execute_reorientation)
        return state.kernel.activation.status()

    @app.post("/api/reorientation", dependencies=[Depends(authorize)])
    def submit_reorientation(request: ReorientationAssessmentRequest):
        state.kernel.activation.submit_assessment(
            request.assessment,
            open_commitment_ids=(
                item.commitment_id
                for item in state.interface.commitments.values()
                if item.state == "open"
            ),
            existing_work_item_ids=state.kernel.work_items,
            session_index_listed_to_end=False,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return state.kernel.activation.status()

    @app.post(
        "/api/reorientation/revision",
        dependencies=[Depends(authorize)],
    )
    def revise_reorientation(request: ReorientationRevisionRequest):
        state.kernel.activation.request_assessment_revision(
            request.reason_code,
            requested_by=request.requested_by,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        return state.kernel.activation.status()

    @app.post("/api/reorientation/queries", dependencies=[Depends(authorize)])
    def resolve_reorientation_query(request: HistoryQueryRequest):
        return HistoryToolService(
            kernel=state.kernel,
            reader=state.history_reader,
            max_result_bytes=state.history_max_result_bytes,
        ).resolve(
            request.action,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )

    @app.post("/api/reorientation/approval", dependencies=[Depends(authorize)])
    def approve_reorientation(request: ReorientationApprovalRequest):
        assessment = state.kernel.activation.assessment
        if assessment is None or not assessment.resume_work_item_ids:
            raise InvariantViolation(
                "Interface activity start requires at least one assessed real WorkItem"
            )
        if state.coding_pilot_id is None:
            raise InvariantViolation("production coding Pilot is not configured")
        if request.conversation_id not in state.interface.conversations:
            raise InvariantViolation("Conversation not found")
        if request.conversation_id != assessment.conversation_id:
            raise InvariantViolation(
                "owner approval must use the assessed canonical Conversation"
            )
        for work_item_id in assessment.resume_work_item_ids:
            state.kernel.validate_owner_confirmed_resume(work_item_id)
        bindings = tuple(
            PilotBinding(
                node_id=node_id,
                pilot_id=state.coding_pilot_id,
                pilot_host_id=state.pilot_hosts.expected_identity.pilot_host_id,
            )
            for node_id in sorted(
                {
                    state.kernel.work_items[item_id].delegated_to_node_id
                    for item_id in assessment.resume_work_item_ids
                }
            )
        )
        resume_ids = frozenset(assessment.resume_work_item_ids)
        state.dispatcher.preflight_ready(
            bindings,
            allowed_work_item_ids=resume_ids,
            allow_owner_confirmed_prepare=True,
        )
        for index, correction in enumerate(request.corrections):
            state.interface.record_owner_correction(
                conversation_id=request.conversation_id,
                statement=correction,
                actor_id=request.actor_id,
                idempotency_key=f"{request.idempotency_key}:correction:{index}",
            )
        for index, work_item_id in enumerate(assessment.resume_work_item_ids):
            state.kernel.prepare_owner_confirmed_resume(
                work_item_id,
                actor_id=request.actor_id,
                idempotency_key=f"{request.idempotency_key}:prepare:{index}",
            )
        state.kernel.activation.approve(
            request.assessment_id,
            actor_id=request.actor_id,
            idempotency_key=request.idempotency_key,
        )
        batch = state.dispatcher.dispatch_ready(
            bindings,
            actor_id=request.actor_id,
            idempotency_key=f"{request.idempotency_key}:dispatch",
            allowed_work_item_ids=resume_ids,
        )
        if not batch.assignments:
            raise InvariantViolation(
                "owner approval did not start a real WorkItem"
            )
        return {
            **state.kernel.activation.status().model_dump(mode="json"),
            "dispatch_batch": batch.model_dump(mode="json"),
        }

    @app.get("/api/activation/status", dependencies=[Depends(authorize)])
    def activation_status():
        return state.kernel.activation.status()

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
                snapshot.snapshot_id: (
                    None
                    if snapshot.state is RouteSnapshotState.RETIRED
                    else state.router.scores(snapshot.candidate_keys)
                )
                for snapshot in snapshots
            },
            "routable": {
                snapshot.snapshot_id: (
                    snapshot.state is RouteSnapshotState.PUBLISHED
                )
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
        require_coding_route_candidate_keys(
            snapshot.route_key,
            snapshot.candidate_keys,
            state.model_registry,
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

    @app.post(
        "/api/route-snapshots/{snapshot_id}/retirements",
        dependencies=[Depends(authorize)],
        status_code=201,
    )
    def retire_route_snapshot(
        snapshot_id: str, request: RetireRouteSnapshotRequest
    ):
        if (
            request.reason_code
            is RouteSnapshotRetirementReason.SUPERSEDED_BY_APPROVED_SNAPSHOT
        ):
            replacement = state.kernel.route_snapshots.get(
                request.replacement_snapshot_id
            )
            if replacement is None:
                raise InvariantViolation("replacement RouteSnapshot not found")
            if (
                replacement.evidence_cursor
                != state.routing_evidence.evidence_cursor
            ):
                raise InvariantViolation(
                    "replacement RouteSnapshot evidence_cursor must equal current "
                    "verified evidence cursor"
                )
            unknown = set(replacement.candidate_keys) - set(state.model_registry)
            if unknown:
                raise InvariantViolation(
                    "replacement RouteSnapshot references unregistered "
                    f"ModelCandidates: {sorted(unknown)}"
                )
            require_coding_route_candidate_keys(
                replacement.route_key,
                replacement.candidate_keys,
                state.model_registry,
            )
        state.kernel.retire_route_snapshot(
            snapshot_id,
            reason_code=request.reason_code,
            replacement_snapshot_id=request.replacement_snapshot_id,
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
        if (
            websocket.headers.get("authorization")
            != f"Bearer {state.api_bearer_token}"
            or websocket.headers.get("x-nanihold-device-id")
            not in state.authorized_device_ids
            or websocket.headers.get("origin") not in allowed_origins
        ):
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
