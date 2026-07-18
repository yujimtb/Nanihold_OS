"""Nanihold から LETHE supplemental API へ渡すレコード契約。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunHeader(_StrictModel):
    run_id: str = Field(min_length=1, max_length=64)
    started_at: str = Field(min_length=1)
    ended_at: str = Field(min_length=1)
    task_id: str | None = None
    task_description: str | None = None


class NodeConsumption(_StrictModel):
    node_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    consumed: dict[str, float]


class AccountingPayload(_StrictModel):
    header: RunHeader
    node_consumption: list[NodeConsumption]
    run_consumption: dict[str, float]
    result_state: Literal["completed", "cancelled", "failed", "stopped"]
    event_count: int = Field(ge=1)


class MemoryPayload(_StrictModel):
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    seq: int = Field(ge=0)
    node_id: str | None = None
    actor_type: str = Field(min_length=1)
    actor_id: str | None = None
    content: dict[str, Any]


class AccountingRecord(_StrictModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1)
    record_kind: Literal["run_accounting"] = "run_accounting"
    source: Literal["nanihold"] = "nanihold"
    run_id: str = Field(min_length=1, max_length=64)
    occurred_at: str = Field(min_length=1)
    text: str = Field(min_length=1)
    payload: AccountingPayload


class MemoryRecord(_StrictModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1)
    record_kind: Literal["run_memory"] = "run_memory"
    source: Literal["nanihold"] = "nanihold"
    run_id: str = Field(min_length=1, max_length=64)
    occurred_at: str = Field(min_length=1)
    text: str = Field(min_length=1)
    payload: MemoryPayload


SupplementalRecord = AccountingRecord | MemoryRecord
SUPPLEMENTAL_RECORD_ADAPTER = TypeAdapter(SupplementalRecord)


class SearchResponse(_StrictModel):
    records: list[SupplementalRecord]

