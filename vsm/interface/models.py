from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import Field

from vsm.kernel.models import (
    BlobRef,
    CompletionEvidence,
    EffectLease,
    Identifier,
    NonBlank,
    StrictModel,
    WorkItem,
)
from vsm.activation.models import ReorientationAssessment

Surface = Literal[
    "web",
    "tui",
    "claude_native",
    "slack",
    "discord",
    "intercom",
    "codex",
]


class Conversation(StrictModel):
    conversation_id: Identifier
    data_space_id: Identifier
    interface_node_id: Identifier
    owner_id: Identifier
    title: NonBlank


class SurfaceBinding(StrictModel):
    binding_id: Identifier
    conversation_id: Identifier
    surface: Surface
    source_session_id: NonBlank
    channel_id: NonBlank
    device_id: NonBlank


class PilotSession(StrictModel):
    pilot_session_id: Identifier
    conversation_id: Identifier
    pilot_id: Identifier
    root_provider_session_id: NonBlank
    provider_session_id: NonBlank
    last_event_cursor: Annotated[int, Field(ge=0)]


class MessageSource(StrictModel):
    surface: Surface
    source_session_id: NonBlank
    source_message_id: NonBlank
    author_id: NonBlank
    channel_id: NonBlank
    occurred_at: datetime


class OwnerMessageAction(StrictModel):
    action_id: Identifier
    idempotency_key: NonBlank
    kind: Literal["owner_message"]
    text: NonBlank
    source: MessageSource


class ConversationMessage(StrictModel):
    message_id: Identifier
    conversation_id: Identifier
    role: Literal["owner", "interface"]
    display_text: NonBlank | None
    blob_ref: BlobRef | None
    occurred_at: datetime
    source: MessageSource | None


class ConversationActionReceipt(StrictModel):
    action_id: Identifier
    conversation_id: Identifier
    status: Literal["accepted", "completed", "failed"]
    owner_message_id: Identifier
    interface_message: ConversationMessage | None
    event_cursor: Annotated[int, Field(gt=0)]
    error: str | None


class ConversationCreatedReceipt(StrictModel):
    conversation_id: Identifier
    surface_binding_id: Identifier
    event_cursor: Annotated[int, Field(gt=0)]


class CreateWorkItemAction(StrictModel):
    action_id: Identifier
    kind: Literal["work_item.create"]
    work_item: WorkItem
    depends_on_work_item_ids: tuple[Identifier, ...]


class DelegateWorkItemAction(StrictModel):
    action_id: Identifier
    kind: Literal["work_item.delegate"]
    work_item_id: Identifier
    delegated_to_node_id: Identifier


class RecordDecisionAction(StrictModel):
    action_id: Identifier
    kind: Literal["decision.record"]
    statement: NonBlank
    supersedes_decision_id: Identifier | None


class UpdateCommitmentAction(StrictModel):
    action_id: Identifier
    kind: Literal["commitment.update"]
    commitment_id: Identifier
    statement: NonBlank
    work_item_id: Identifier | None
    state: Literal["open", "satisfied", "withdrawn"]


class ReadHistoryAction(StrictModel):
    action_id: Identifier
    kind: Literal["history.read"]
    operation: Literal[
        "list_sessions",
        "read_timeline",
        "read_raw",
        "search",
        "resolve_reference",
        "list_open_commitments",
        "get_current_state",
    ]
    argument: NonBlank | None
    page_cursor: NonBlank | None


class SubmitReorientationAction(StrictModel):
    action_id: Identifier
    kind: Literal["reorientation.submit"]
    assessment: ReorientationAssessment


class SelectPilotAction(StrictModel):
    action_id: Identifier
    kind: Literal["pilot.select"]
    work_item_id: Identifier
    route_key: NonBlank


class PlanEffectAction(StrictModel):
    action_id: Identifier
    kind: Literal["effect.plan"]
    effect_lease: EffectLease


class ProposeCompletionAction(StrictModel):
    action_id: Identifier
    kind: Literal["completion.propose"]
    work_item_id: Identifier
    evidence: CompletionEvidence


InterfaceAction: TypeAlias = Annotated[
    CreateWorkItemAction
    | DelegateWorkItemAction
    | RecordDecisionAction
    | UpdateCommitmentAction
    | ReadHistoryAction
    | SubmitReorientationAction
    | SelectPilotAction
    | PlanEffectAction
    | ProposeCompletionAction,
    Field(discriminator="kind"),
]


class Commitment(StrictModel):
    commitment_id: Identifier
    conversation_id: Identifier
    statement: NonBlank
    work_item_id: Identifier | None
    state: Literal["open", "satisfied", "withdrawn"]


class Decision(StrictModel):
    decision_id: Identifier
    conversation_id: Identifier
    statement: NonBlank
    supersedes_decision_id: Identifier | None


class NodeMemory(StrictModel):
    memory_id: Identifier
    node_id: Identifier
    statement: NonBlank
    source_blob_ref: BlobRef


class ConversationStatus(StrictModel):
    conversation: Conversation
    surface_bindings: tuple[SurfaceBinding, ...]
    pilot_sessions: tuple[PilotSession, ...]
    open_commitments: tuple[Commitment, ...]
    unfinished_work_item_ids: tuple[Identifier, ...]
    active_execution_ids: tuple[Identifier, ...]
    last_messages: tuple[ConversationMessage, ...]
    model_calls: Literal[0]
