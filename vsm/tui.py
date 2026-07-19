"""Deterministic, ANSI-free terminal rendering for the owner interface."""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import wrap
from typing import Any

import httpx

from vsm.activation.models import ActivationState, ActivationStatus
from vsm.errors import InvariantViolation

_STATES = tuple(ActivationState)
_STATE_LABELS = {
    ActivationState.UNCOMMISSIONED: "履歴取込待ち",
    ActivationState.HISTORY_IMPORTED: "履歴検証済み",
    ActivationState.REORIENTATION_ONLY: "Fable読解中",
    ActivationState.AWAITING_OWNER_CONFIRMATION: "owner確認待ち",
    ActivationState.ACTIVE: "活動中",
}


@dataclass(frozen=True, slots=True)
class TuiOperationalSnapshot:
    activation: ActivationStatus
    current_work: tuple[str, ...]
    waiting_work: tuple[str, ...]
    delegations: tuple[str, ...]
    cost_usd: float | None
    quota: str | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("cost_usd must be non-negative")
        if self.quota is not None and not self.quota.strip():
            raise ValueError("quota must be non-blank or None")


def load_operational_snapshot(client: httpx.Client) -> TuiOperationalSnapshot:
    """Read model-free operational projections through the public API."""
    activation = ActivationStatus.model_validate(
        _get_json(client, "/api/activation/status")
    )
    work_document = _object(
        _get_json(client, "/api/work-items"), "/api/work-items"
    )
    execution_document = _object(
        _get_json(client, "/api/executions"), "/api/executions"
    )
    work_items = _object_list(work_document.get("items"), "work-items.items")
    executions = _object_list(
        execution_document.get("items"), "executions.items"
    )

    current_work = tuple(
        _work_label(item)
        for item in work_items
        if item.get("state") in {"ready", "active"}
    )
    waiting_work = tuple(
        _work_label(item)
        for item in work_items
        if item.get("state") in {"proposed", "paused", "blocked"}
    )
    delegations = tuple(
        f"{_required_text(item, 'title')} → "
        f"{_required_text(item, 'delegated_to_node_id')}"
        for item in work_items
        if _required_text(item, "delegated_to_node_id")
        != _required_text(item, "owner_node_id")
    )
    active_execution_ids = tuple(
        _required_text(item, "execution_id")
        for item in executions
        if item.get("state") in {"requested", "active", "paused"}
    )
    if active_execution_ids:
        current_work = (
            *current_work,
            *(f"Execution {execution_id}" for execution_id in active_execution_ids),
        )

    assessment = activation.assessment
    evidence_refs = (
        ()
        if assessment is None
        else tuple(citation.evidence_ref for citation in assessment.citations)
    )
    return TuiOperationalSnapshot(
        activation=activation,
        current_work=current_work,
        waiting_work=waiting_work,
        delegations=delegations,
        cost_usd=None,
        quota=None,
        evidence_refs=evidence_refs,
    )


def render_dashboard(snapshot: TuiOperationalSnapshot, *, width: int = 88) -> str:
    """Render the same activation and operational hierarchy as the Web UI."""
    if width < 60:
        raise ValueError("TUI width must be at least 60")
    activation = snapshot.activation
    lines = [
        _rule(" Nanihold / Fable ", width),
        f"状態: {activation.state.value} — {_STATE_LABELS[activation.state]}",
        _progress(activation.state),
        "",
    ]
    receipt = activation.import_receipt
    if receipt is not None:
        records = sum(source.record_count for source in receipt.sources)
        contributing_sources = sum(
            source.record_count > 0 for source in receipt.sources
        )
        lines.append(
            "履歴: "
            f"{records:,} records / {contributing_sources} sources "
            f"({len(receipt.sources)} required) / "
            f"inventory={receipt.inventory_id}"
        )
    lines.append(
        "再読解: "
        f"{activation.reorientation_pilot_calls} calls / "
        f"{activation.reorientation_input_tokens:,} input / "
        f"{activation.reorientation_output_tokens:,} output"
    )
    lines.extend(
        [
            "",
            *_section("現在", snapshot.current_work, width),
            *_section("待ち", snapshot.waiting_work, width),
            *_section("委任", snapshot.delegations, width),
            (
                "費用・quota: "
                f"{_cost(snapshot.cost_usd)} / {snapshot.quota or 'telemetry待ち'}"
            ),
            f"根拠: {len(snapshot.evidence_refs)} refs",
        ]
    )
    assessment = activation.assessment
    if assessment is not None:
        lines.extend(
            [
                "",
                _rule(" Fableが追いつきました ", width),
                *_wrapped("理解", assessment.understanding, width),
                *_section("ミッション", assessment.active_missions, width),
                *_section(
                    "決定・制約",
                    assessment.decisions_and_constraints,
                    width,
                ),
                *_section("不明点", assessment.unknowns, width),
                *_section(
                    "再開候補",
                    assessment.resume_work_item_ids,
                    width,
                ),
                (
                    "coverage: "
                    f"{len(assessment.covered_session_ids)} sessions / "
                    f"history#{assessment.history_cursor} / "
                    f"current#{assessment.current_state_cursor}"
                ),
                f"citations: {len(assessment.citations)}",
            ]
        )
    if activation.state is ActivationState.AWAITING_OWNER_CONFIRMATION:
        lines.extend(
            [
                "",
                "次の操作: Web UIまたはapproval commandで訂正を保存し、",
                "          assessmentをowner承認してください。",
            ]
        )
    elif (
        activation.state is ActivationState.REORIENTATION_ONLY
        and activation.reorientation_error is not None
    ):
        lines.extend(
            [
                "",
                f"履歴読解は安全に停止: {activation.reorientation_error}",
                "次の操作: `vsm fable catch-up` またはWeb UIから再開してください。",
                "安全ゲート: ExecutionとEffectは開始されていません。",
            ]
        )
    elif activation.state is not ActivationState.ACTIVE:
        lines.extend(
            [
                "",
                "安全ゲート: owner承認までExecutionとEffectは開始されません。",
            ]
        )
    return "\n".join(lines) + "\n"


def _progress(current: ActivationState) -> str:
    current_index = _STATES.index(current)
    values: list[str] = []
    for index, state in enumerate(_STATES):
        marker = "✓" if index < current_index else "●" if index == current_index else "○"
        values.append(f"{marker}{state.value}")
    return " > ".join(values)


def _section(title: str, values: tuple[str, ...], width: int) -> list[str]:
    if not values:
        return [f"{title}: —"]
    lines = [f"{title}: {len(values)}"]
    for value in values:
        wrapped = wrap(
            value,
            width=max(20, width - 4),
            break_long_words=False,
            break_on_hyphens=False,
        ) or [""]
        lines.append(f"  - {wrapped[0]}")
        lines.extend(f"    {item}" for item in wrapped[1:])
    return lines


def _wrapped(title: str, value: str, width: int) -> list[str]:
    wrapped = wrap(
        value,
        width=max(20, width - len(title) - 2),
        break_long_words=False,
        break_on_hyphens=False,
    ) or [""]
    return [f"{title}: {wrapped[0]}", *(f"  {item}" for item in wrapped[1:])]


def _rule(label: str, width: int) -> str:
    if len(label) >= width:
        return label[:width]
    left = (width - len(label)) // 2
    return "─" * left + label + "─" * (width - len(label) - left)


def _cost(value: float | None) -> str:
    return "未計測" if value is None else f"${value:.4f}"


def _get_json(client: httpx.Client, path: str) -> Any:
    response = client.get(path)
    if not response.is_success:
        raise InvariantViolation(
            f"Nanihold API HTTP {response.status_code} for {path}: {response.text}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise InvariantViolation(f"Nanihold API returned invalid JSON for {path}") from exc


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvariantViolation(f"{label} must be a JSON object")
    return value


def _object_list(value: Any, label: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise InvariantViolation(f"{label} must be a JSON object list")
    return tuple(value)


def _required_text(item: dict[str, Any], field: str) -> str:
    value = item.get(field)
    if not isinstance(value, str) or not value:
        raise InvariantViolation(f"operational projection lacks {field}")
    return value


def _work_label(item: dict[str, Any]) -> str:
    return f"{_required_text(item, 'title')} [{_required_text(item, 'state')}]"
