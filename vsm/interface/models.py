from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from vsm.kernel.models import BlobRef, Identifier, NonBlank


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Conversation(StrictModel):
    conversation_id: Identifier
    data_space_id: Identifier
    interface_node_id: Identifier
    owner_id: Identifier
    provider_session_id: NonBlank | None
    last_event_cursor: Annotated[int, Field(ge=0)]
    status: Literal["active", "paused", "closed"]


class ConversationMessage(StrictModel):
    message_id: Identifier
    conversation_id: Identifier
    role: Literal["owner", "interface"]
    display_text: NonBlank | None
    blob_ref: BlobRef | None
    occurred_at: datetime


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
    open_commitments: tuple[Commitment, ...]
    unfinished_work_item_ids: tuple[Identifier, ...]
    active_execution_ids: tuple[Identifier, ...]
    last_messages: tuple[ConversationMessage, ...]
    model_calls: Literal[0]
