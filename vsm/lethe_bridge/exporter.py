"""Event Log と Run 会計から LETHE supplemental records を構築する。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from vsm.lethe_bridge.models import (
    AccountingPayload,
    AccountingRecord,
    MemoryPayload,
    MemoryRecord,
    NodeConsumption,
    RunHeader,
)

MEMORY_EVENT_TYPES = frozenset(
    {
        "audit_finding",
        "consortium_decided",
        "coordination_decided",
        "coordination_directive",
        "human_review_responded",
        "instruction_received",
        "policy_decision",
        "web_instruction_received",
    }
)


def build_accounting_record(
    *,
    run_id: str,
    ended_at: str,
    events: Sequence[Mapping[str, Any]],
    nodes: Mapping[str, Any],
    node_run_states: Mapping[tuple[str, str], Any],
    run_consumption: Mapping[str, float],
) -> AccountingRecord:
    if not events:
        raise ValueError("accounting export requires at least one Run event")
    first = events[0]
    task_payload = next(
        (
            event.get("payload")
            for event in events
            if event.get("event_type") == "task_submitted"
        ),
        None,
    )
    if task_payload is not None and not isinstance(task_payload, Mapping):
        raise ValueError("task_submitted payload must be an object")

    consumption: list[NodeConsumption] = []
    for (state_run_id, node_id), state in sorted(node_run_states.items()):
        if state_run_id != run_id:
            continue
        node = nodes.get(node_id)
        if node is None:
            raise ValueError(f"NodeRunState has no Node: {node_id}")
        role_value = getattr(node.vsm_position, "value", node.vsm_position)
        consumption.append(
            NodeConsumption(
                node_id=node_id,
                role=str(role_value),
                consumed={
                    key: float(value)
                    for key, value in state.cost_consumed.items()
                },
            )
        )

    result_state = _result_state(events)
    header = RunHeader(
        run_id=run_id,
        started_at=_required_string(first, "ts"),
        ended_at=ended_at,
        task_id=(
            _optional_string(task_payload, "task_id")
            if task_payload is not None
            else None
        ),
        task_description=(
            _optional_string(task_payload, "description")
            if task_payload is not None
            else None
        ),
    )
    return AccountingRecord(
        record_id=f"nanihold:{run_id}:accounting",
        run_id=run_id,
        occurred_at=ended_at,
        text=f"Nanihold Run {run_id} result={result_state}",
        payload=AccountingPayload(
            header=header,
            node_consumption=consumption,
            run_consumption={key: float(value) for key, value in run_consumption.items()},
            result_state=result_state,
            event_count=len(events),
        ),
    )


def build_memory_records(
    *, run_id: str, events: Sequence[Mapping[str, Any]]
) -> list[MemoryRecord]:
    records: list[MemoryRecord] = []
    for event in events:
        event_type = event.get("event_type")
        if event_type not in MEMORY_EVENT_TYPES:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError(f"{event_type} payload must be an object")
        event_id = _required_string(event, "event_id")
        records.append(
            MemoryRecord(
                record_id=f"nanihold:{run_id}:memory:{event_id}",
                run_id=run_id,
                occurred_at=_required_string(event, "ts"),
                text=_memory_text(str(event_type), payload),
                payload=MemoryPayload(
                    event_id=event_id,
                    event_type=str(event_type),
                    seq=_required_int(event, "seq"),
                    node_id=_optional_string(event, "node_id"),
                    actor_type=_required_string(event, "actor_type"),
                    actor_id=_optional_string(event, "actor_id"),
                    content=dict(payload),
                ),
            )
        )
    return records


def _result_state(events: Sequence[Mapping[str, Any]]) -> str:
    event_types = {str(event.get("event_type", "")) for event in events}
    if event_types & {"s1_completion", "web_run_completed", "node_completed"}:
        return "completed"
    if event_types & {"web_run_cancelled", "node_terminated"}:
        return "cancelled"
    if (
        any(event_type.endswith("_error") for event_type in event_types)
        or event_types & {
            "instruction_failed",
            "llm_timeout",
            "node_failed",
            "tool_failed",
        }
    ):
        return "failed"
    return "stopped"


def _memory_text(event_type: str, payload: Mapping[str, Any]) -> str:
    keys_by_type = {
        "audit_finding": ("content",),
        "consortium_decided": ("decision", "reason", "dissent_summary"),
        "coordination_decided": ("decision", "reason"),
        "coordination_directive": ("directive",),
        "human_review_responded": ("response", "decision"),
        "instruction_received": ("instruction",),
        "policy_decision": ("directive", "followup_request"),
        "web_instruction_received": ("instruction",),
    }
    values = [
        value.strip()
        for key in keys_by_type[event_type]
        for value in [payload.get(key)]
        if isinstance(value, str) and value.strip()
    ]
    if not values:
        raise ValueError(f"{event_type} has no exportable text")
    return " / ".join(values)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be a non-empty string when present")
    return item


def _required_int(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return item
