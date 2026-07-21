"""ACR-08 real-path test matrix, dry-run plan, and evidence verification.

This module is intentionally side-effect free.  It enumerates the complete
Discord/Slack matrix and validates evidence collected from the Nanihold
Operational Ledger and existing ACR-04 audit-trace endpoints.  It never sends
to Discord or Slack.  The command-line wrapper can therefore be used to
prepare a run before an owner-approved live operation.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

CHANNELS = ("discord", "slack")
RESOLUTION_MODES = (
    "explicit_mention",
    "bot_reply_attributed",
    "bot_reply_unattributed",
    "thread_inheritance",
    "observation_only",
)
RECIPIENT_KINDS = ("nagi", "task_agent")
DIRECTIONS = ("owner_to_agent", "agent_to_owner", "agent_to_agent")

MATRIX_VERSION = "acr-08/v1"
DATA_SPACE_ID = "space:personal-primary"

_RESOLUTION_LABELS = {
    "explicit_mention": "文頭 @名前",
    "bot_reply_attributed": "bot 配信への返信（帰属あり）",
    "bot_reply_unattributed": "帰属不能な bot 配信への返信 → Nagi",
    "thread_inheritance": "スレッド継承",
    "observation_only": "非合致 → 観測のみ",
}
_ROLE_LABELS = {"nagi": "Nagi(S5)", "task_agent": "タスク実行エージェント"}
_DIRECTION_LABELS = {
    "owner_to_agent": "オーナー → エージェント(通知)",
    "agent_to_owner": "エージェント → オーナー(draft→承認→配信)",
    "agent_to_agent": "エージェント → エージェント(ACR-07)",
}


def _cell_id(channel: str, resolution: str, role: str, direction: str) -> str:
    return "ACR08-{}-{}-{}-{}".format(
        channel.upper(), resolution.upper(), role.upper(), direction.upper()
    )


def _owner_text(channel: str, resolution: str, role: str, direction: str) -> str:
    """Return the exact text used by the owner checklist for an applicable cell."""

    prefix = f"ACR08 {channel} {resolution} {role}"
    if direction == "owner_to_agent":
        if resolution == "explicit_mention":
            name = "Nagi" if role == "nagi" else "<割当済み個名>"
            return f"@{name} {prefix}"
        if resolution == "bot_reply_attributed":
            return f"{prefix} bot attribution reply"
        if resolution == "bot_reply_unattributed":
            return f"{prefix} unattributed bot reply"
        if resolution == "thread_inheritance":
            return f"{prefix} inherited thread reply"
        return f"{prefix} observation-only message"
    if direction == "agent_to_owner":
        return f"{prefix} reply-draft body"
    return f"{prefix} internal message"


def _cell(
    *,
    channel: str,
    resolution: str,
    role: str,
    direction: str,
    applicable: bool,
    mode: str,
    na_reason: str | None = None,
    expected_target: str | None = None,
    verification: tuple[str, ...] = (),
    owner_text: str | None = None,
    note: str | None = None,
) -> dict[str, object]:
    if applicable == (na_reason is not None):
        raise ValueError("applicable cells must have no N/A reason and vice versa")
    if applicable and not verification:
        raise ValueError("applicable cells require verification points")
    return {
        "cell_id": _cell_id(channel, resolution, role, direction),
        "channel": channel,
        "resolution": resolution,
        "resolution_label": _RESOLUTION_LABELS[resolution],
        "recipient_kind": role,
        "recipient_kind_label": _ROLE_LABELS[role],
        "direction": direction,
        "direction_label": _DIRECTION_LABELS[direction],
        "applicable": applicable,
        "execution_mode": mode,
        "na_reason": na_reason,
        "expected_target": expected_target,
        "verification": list(verification),
        "owner_text": owner_text,
        "note": note,
    }


def build_matrix() -> tuple[dict[str, object], ...]:
    """Build the authoritative 2 × 5 × 2 × 3 = 60 cell matrix.

    The recipient-kind axis is also used as the endpoint-kind axis for the two
    outbound directions.  This preserves the specified Cartesian product;
    `recipient_kind_label` is intentionally retained so that the same cell ID
    is usable in the owner checklist and in evidence submitted by the runtime.
    """

    cells: list[dict[str, object]] = []
    for channel in CHANNELS:
        for resolution in RESOLUTION_MODES:
            for role in RECIPIENT_KINDS:
                for direction in DIRECTIONS:
                    applicable = False
                    execution_mode = "not_applicable"
                    na_reason: str | None = None
                    expected_target: str | None = None
                    verification: tuple[str, ...] = ()
                    owner_text: str | None = None
                    note: str | None = None

                    if direction == "owner_to_agent":
                        if resolution == "explicit_mention":
                            applicable = True
                            execution_mode = "owner_checklist"
                            expected_target = "Nagi" if role == "nagi" else "assigned_agent_name"
                            verification = (
                                "recipient_agent_name matches the addressed name",
                                "agent_notification_delivered exists in the Operational Ledger",
                                "ACR-04 notification trace is verified",
                            )
                        elif resolution == "bot_reply_attributed":
                            applicable = True
                            execution_mode = "owner_checklist"
                            expected_target = "Nagi" if role == "nagi" else "assigned_agent_name"
                            verification = (
                                "bot message attribution is preserved",
                                "recipient_agent_name matches the bot attribution",
                                "ACR-04 notification trace is verified",
                            )
                        elif resolution == "bot_reply_unattributed" and role == "nagi":
                            applicable = True
                            execution_mode = "owner_checklist"
                            expected_target = "Nagi"
                            verification = (
                                "unattributed bot reply is routed to Nagi",
                                "agent_notification_delivered exists in the Operational Ledger",
                                "ACR-04 notification trace is verified",
                            )
                        elif resolution == "thread_inheritance":
                            applicable = True
                            execution_mode = "owner_checklist"
                            expected_target = "Nagi" if role == "nagi" else "assigned_agent_name"
                            verification = (
                                "recipient_agent_name matches the parent thread target",
                                "agent_notification_delivered exists in the Operational Ledger",
                                "ACR-04 notification trace is verified",
                            )
                        elif resolution == "observation_only" and role == "nagi":
                            applicable = True
                            execution_mode = "owner_checklist"
                            expected_target = None
                            verification = (
                                "the source is retained as a LETHE observation",
                                "no agent_notification_delivered exists for the source",
                                "no agent is incorrectly addressed",
                            )

                    elif direction == "agent_to_owner":
                        # The five destination-resolution modes are inbound
                        # concepts.  One explicit row per endpoint kind is
                        # retained as the outbound draft/approval test cell;
                        # the other four rows are N/A within the fixed product.
                        if resolution == "explicit_mention":
                            applicable = True
                            execution_mode = "owner_checklist"
                            expected_target = "owner"
                            verification = (
                                "reply-draft@1 carries the producing agent name",
                                "reply-approval@1 is the approval gate",
                                "approved send-record@1 is anchored to the draft",
                            )
                            note = "recipient_kind denotes the draft author for outbound direction"

                    else:  # agent_to_agent
                        if resolution == "explicit_mention":
                            applicable = True
                            execution_mode = "automated_dry_run"
                            expected_target = "registered_agent_name"
                            verification = (
                                "sender and recipient are AgentNameRegistry-issued names",
                                "agent_notification_delivered uses the shared Ledger path",
                                "WorkItem and Execution references are present",
                            )
                            note = "recipient_kind denotes the internal endpoint kind"
                        elif resolution == "observation_only" and role == "nagi":
                            applicable = True
                            execution_mode = "automated_dry_run"
                            expected_target = None
                            verification = (
                                "an unaddressed internal probe is not delivered",
                                "no external channel send is attempted",
                                "the negative-control result is recorded",
                            )
                            note = "negative control; Nagi is a neutral endpoint label"

                    if not applicable:
                        if direction == "agent_to_owner":
                            na_reason = "宛先解決モードは inbound 専用で、返信は draft/approval 契約で検証するため"
                        elif direction == "agent_to_agent" and resolution in {
                            "bot_reply_attributed",
                            "bot_reply_unattributed",
                            "thread_inheritance",
                        }:
                            na_reason = "bot返信・スレッド継承はチャネル着信の文脈であり、内部個名通信には適用しないため"
                        elif direction == "agent_to_agent" and resolution == "observation_only":
                            na_reason = "観測のみの負の制御はNagiラベルの1セルだけを代表実施するため"
                        elif direction == "owner_to_agent" and resolution == "bot_reply_unattributed":
                            na_reason = "帰属不能なbot返信はNagiへ集約され、タスク実行エージェント宛てにはならないため"
                        elif direction == "owner_to_agent" and resolution == "observation_only":
                            na_reason = "非合致は宛先を持たず観測のみであり、タスク実行エージェント種別を適用できないため"
                        else:
                            na_reason = "固定直積上の同一経路重複を避ける境界セルであり、方向の契約に適用しないため"

                    if applicable and execution_mode == "owner_checklist":
                        owner_text = _owner_text(channel, resolution, role, direction)
                    cells.append(
                        _cell(
                            channel=channel,
                            resolution=resolution,
                            role=role,
                            direction=direction,
                            applicable=applicable,
                            mode=execution_mode,
                            na_reason=na_reason,
                            expected_target=expected_target,
                            verification=verification,
                            owner_text=owner_text,
                            note=note,
                        )
                    )

    if len(cells) != 60:
        raise AssertionError(f"ACR-08 matrix must contain 60 cells, got {len(cells)}")
    if sum(bool(cell["applicable"]) for cell in cells) != 26:
        raise AssertionError("ACR-08 matrix must contain exactly 26 applicable cells")
    if sum(cell["execution_mode"] == "owner_checklist" for cell in cells) != 20:
        raise AssertionError("ACR-08 owner checklist must contain exactly 20 cells")
    if sum(cell["execution_mode"] == "automated_dry_run" for cell in cells) != 6:
        raise AssertionError("ACR-08 automation plan must contain exactly 6 cells")
    return tuple(cells)


def matrix_manifest() -> dict[str, object]:
    cells = build_matrix()
    return {
        "schema": "nanihold/acr08-matrix",
        "matrix_version": MATRIX_VERSION,
        "axes": {
            "channel": list(CHANNELS),
            "resolution": list(RESOLUTION_MODES),
            "recipient_kind": list(RECIPIENT_KINDS),
            "direction": list(DIRECTIONS),
        },
        "counts": {
            "total": len(cells),
            "applicable": sum(bool(cell["applicable"]) for cell in cells),
            "na": sum(not bool(cell["applicable"]) for cell in cells),
            "owner_checklist": sum(
                cell["execution_mode"] == "owner_checklist" for cell in cells
            ),
            "automated_dry_run": sum(
                cell["execution_mode"] == "automated_dry_run" for cell in cells
            ),
        },
        "real_external_sends_performed": False,
        "cells": list(cells),
    }


def render_matrix_markdown() -> str:
    lines = [
        "# ACR-08 実経路疎通試験マトリクス",
        "",
        "この成果物は `2 × 5 × 2 × 3 = 60` セルを列挙する。適用セルは26、N/Aセルは34。",
        "このWorkItemでは実Discord/実Slackへの実送信を行わない。N/Aセルには理由を明記する。",
        "",
        "| # | Cell ID | チャネル | 宛先解決 | 宛先種別/端点種別 | 方向 | 判定 | 実施形態 | N/A理由 |",
        "|---:|---|---|---|---|---|---|---|---|",
    ]
    for index, cell in enumerate(build_matrix(), 1):
        verdict = "適用" if cell["applicable"] else "N/A"
        reason = str(cell["na_reason"] or "—").replace("|", "\\|")
        lines.append(
            "| {index} | `{cell_id}` | {channel} | {resolution} | {role} | {direction} | {verdict} | {mode} | {reason} |".format(
                index=index,
                cell_id=cell["cell_id"],
                channel=cell["channel"],
                resolution=cell["resolution"],
                role=cell["recipient_kind"],
                direction=cell["direction"],
                verdict=verdict,
                mode=cell["execution_mode"],
                reason=reason,
            )
        )
    lines.extend(
        [
            "",
            "## 判定の読み方",
            "",
            "`owner_to_agent` は宛先種別を受信者として扱う。`agent_to_owner` は宛先種別を起草者種別として扱い、",
            "`agent_to_agent` は内部通信の端点種別として扱う。これは固定直積60セルを崩さず、各方向の契約を明示するための表記である。",
            "",
            "適用セルの検証観点は、配送先の正しさ、Operational Ledger Event、個名帰属、既存AＣR-04トレース、",
            "返信系の `reply-approval@1` ゲート、観測のみの誤配送ゼロである。",
            "",
        ]
    )
    return "\n".join(lines)


def render_owner_checklist() -> str:
    cells = [
        cell for cell in build_matrix() if cell["execution_mode"] == "owner_checklist"
    ]
    lines = [
        "# ACR-08 オーナー実施チェックリスト",
        "",
        "このチェックリストは人間操作が必要な20セル（owner→agent 16、agent→owner 4）を対象にする。",
        "実Discord/実Slackへの送信はオーナー承認後に別途実施し、このWorkItemの実行では行わない。",
        "各行の送る文言はそのまま使い、実施後に `results.json` へ実際のmessage/notification/draft IDを記録する。",
        "",
        "## 共通の記録欄",
        "",
        "- `cell_id`: 下表の値（変更しない）",
        "- `status`: `passed` / `failed` / `pending`",
        "- 通知系: `source_message_id`, `notification_id`, `audit_trace_subject`",
        "- 返信系: `draft_id`, `approval_id`, `send_record_id`, `audit_trace_subject`",
        "- 観測のみ: `observation_subject` と notification が存在しないこと",
        "- すべての結果は、Ledger Eventまたは `vsm audit-trace` の読取結果を根拠にする。",
        "",
        "## 手順",
        "",
    ]
    for index, cell in enumerate(cells, 1):
        cell_id = str(cell["cell_id"])
        direction = str(cell["direction"])
        lines.extend(
            [
                f"### {index}. `{cell_id}`",
                "",
                f"- チャネル: `{cell['channel']}`",
                f"- 宛先解決: `{cell['resolution']}`（{cell['resolution_label']}）",
                f"- 宛先/起草者種別: `{cell['recipient_kind']}`（{cell['recipient_kind_label']}）",
                f"- 送る文言: `{cell['owner_text']}`",
            ]
        )
        if direction == "owner_to_agent":
            lines.extend(
                [
                    "- 操作: 指定チャネルで送信する。bot返信は指定されたbotメッセージへ、スレッド継承は事前に同じ本文の親を作ってから返信する。",
                    f"- 期待結果: `{cell['expected_target'] or 'none'}`への誤配送なし。適用観点: "
                    + " / ".join(str(item) for item in cell["verification"]),
                ]
            )
        else:
            lines.extend(
                [
                    "- 操作: 指定個名のエージェントに本文を起草させ、LETHE card-queueの `reply-draft@1` を確認してから、オーナーが `reply-approval@1` を承認する。",
                    "- 期待結果: 承認前の外部送信は0件。承認後は同じdraftをanchorする `send-record@1` だけが既存ブリッジ経路で記録される。",
                ]
            )
        lines.extend(
            [
                "- 実施結果: `status=` / `source_message_id=` / `notification_id=` / `audit_trace_subject=`",
                "",
            ]
        )
    return "\n".join(lines)


def automation_plan(
    *,
    sender_agent_name: str | None = None,
    recipient_agent_name: str | None = None,
    work_item_id: str | None = None,
    execution_id: str | None = None,
) -> dict[str, object]:
    """Return the six automation cells as dry-run API requests.

    Values may be omitted only for dry-run planning; a live caller must supply
    all four identity/reference values and must still obtain owner approval
    outside this WorkItem.  No method in this module performs HTTP or gateway
    writes.
    """

    cells = [
        cell
        for cell in build_matrix()
        if cell["execution_mode"] == "automated_dry_run"
    ]
    requests: list[dict[str, object]] = []
    for index, cell in enumerate(cells, 1):
        is_negative = cell["resolution"] == "observation_only"
        if is_negative:
            request: dict[str, object] = {
                "method": "GET",
                "path": "/api/events?after_cursor=<cursor>&limit=1000",
                "purpose": "negative-control read; assert no agent_notification_delivered for probe",
            }
        else:
            request = {
                "method": "POST",
                "path": "/api/agent-messages",
                "body": {
                    "data_space_id": DATA_SPACE_ID,
                    "source_instance_id": "acr08-automation",
                    "sender_agent_name": sender_agent_name or "<registered-sender-agent>",
                    "recipient_agent_name": recipient_agent_name or "<registered-recipient-agent>",
                    "source_message_id": f"acr08-automation-{index}",
                    "body": f"@{recipient_agent_name or '<registered-recipient-agent>'} ACR08 agent-to-agent probe {index}",
                    "related_work_item_id": work_item_id or "<work-item-id>",
                    "related_execution_id": execution_id or "<execution-id>",
                    "requires_work_item": False,
                },
                "purpose": "shared ACR-02/ACR-07 Operational Ledger delivery",
            }
        requests.append(
            {
                "step": index,
                "cell_id": cell["cell_id"],
                "channel": cell["channel"],
                "recipient_kind": cell["recipient_kind"],
                "resolution": cell["resolution"],
                "request": request,
                "external_send": False,
                "expected": list(cell["verification"]),
            }
        )
    return {
        "schema": "nanihold/acr08-automation-plan",
        "matrix_version": MATRIX_VERSION,
        "dry_run": True,
        "real_discord_or_slack_send": False,
        "requests": requests,
    }


class EvidenceVerificationError(ValueError):
    """Raised when a submitted ACR-08 result cannot be proven by evidence."""


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise EvidenceVerificationError(f"{label} must be an object")
    return value


def _required_bool(value: Mapping[str, object], *, key: str, label: str) -> bool:
    flag = value.get(key)
    if not isinstance(flag, bool):
        raise EvidenceVerificationError(f"{label}.{key} must be an explicit boolean")
    return flag


def _event_payload(event: Mapping[str, object]) -> Mapping[str, object]:
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        return payload
    nested = event.get("event")
    if isinstance(nested, Mapping):
        nested_payload = nested.get("payload")
        if isinstance(nested_payload, Mapping):
            return nested_payload
    return {}


def _event_type(event: Mapping[str, object]) -> str | None:
    value = event.get("event_type")
    if isinstance(value, str):
        return value
    nested = event.get("event")
    if isinstance(nested, Mapping) and isinstance(nested.get("event_type"), str):
        return str(nested["event_type"])
    return None


def _event_id(event: Mapping[str, object]) -> str | None:
    for candidate in (event.get("event_id"), event.get("id")):
        if isinstance(candidate, str):
            return candidate
    nested = event.get("event")
    if isinstance(nested, Mapping) and isinstance(nested.get("event_id"), str):
        return str(nested["event_id"])
    return None


def _notification_from_event(event: Mapping[str, object]) -> Mapping[str, object] | None:
    payload = _event_payload(event)
    notification = payload.get("notification")
    return notification if isinstance(notification, Mapping) else None


def _ledger_events(evidence: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    raw = evidence.get("ledger_events")
    if not isinstance(raw, list):
        raise EvidenceVerificationError("evidence.ledger_events must be an array")
    return tuple(_mapping(item, label="ledger event") for item in raw)


def _trace_for(
    evidence: Mapping[str, object], result: Mapping[str, object]
) -> Mapping[str, object]:
    trace = result.get("audit_trace")
    if trace is None:
        traces = evidence.get("audit_traces")
        if isinstance(traces, Mapping):
            subject = result.get("audit_trace_subject")
            if isinstance(subject, str):
                trace = traces.get(subject)
    return _mapping(trace, label="audit trace")


def _verify_notification(
    *,
    cell: Mapping[str, object],
    result: Mapping[str, object],
    evidence: Mapping[str, object],
) -> None:
    notification_id = result.get("notification_id")
    if not isinstance(notification_id, str) or not notification_id:
        raise EvidenceVerificationError("routed cell requires notification_id")
    trace = _trace_for(evidence, result)
    if trace.get("verified") is not True:
        raise EvidenceVerificationError("audit trace is not verified")
    expected_target = cell.get("expected_target")
    if expected_target == "Nagi" and trace.get("recipient_agent_name") != "Nagi":
        raise EvidenceVerificationError("notification recipient is not Nagi")
    incoming = _mapping(trace.get("incoming"), label="audit trace incoming")
    expected_channel = result.get("channel")
    if expected_channel != cell.get("channel"):
        raise EvidenceVerificationError("result channel does not match matrix cell")
    if incoming.get("source_platform") not in {None, expected_channel}:
        raise EvidenceVerificationError("audit trace channel does not match result")
    source_message_id = result.get("source_message_id")
    if isinstance(source_message_id, str) and incoming.get("source_message_id") != source_message_id:
        raise EvidenceVerificationError("audit trace source message does not match result")
    delivery = _mapping(trace.get("delivery"), label="audit trace delivery")
    ledger_event_id = delivery.get("ledger_event_id")
    found = False
    for event in _ledger_events(evidence):
        if _event_type(event) != "agent_notification_delivered":
            continue
        notification = _notification_from_event(event)
        if notification is not None and notification.get("notification_id") == notification_id:
            if ledger_event_id is not None and _event_id(event) != ledger_event_id:
                continue
            found = True
            break
    if not found:
        raise EvidenceVerificationError(
            f"Ledger has no matching agent_notification_delivered for {notification_id}"
        )


def _verify_observation_only(
    *, result: Mapping[str, object], evidence: Mapping[str, object]
) -> None:
    subject = result.get("observation_subject")
    if not isinstance(subject, str) or not subject:
        raise EvidenceVerificationError("observation-only cell requires observation_subject")
    observed = evidence.get("observation_subjects")
    if not isinstance(observed, list) or subject not in observed:
        raise EvidenceVerificationError("observation subject is not present in evidence")
    for event in _ledger_events(evidence):
        if _event_type(event) != "agent_notification_delivered":
            continue
        notification = _notification_from_event(event)
        if notification is not None and notification.get("source_observation_subject") == subject:
            raise EvidenceVerificationError("observation-only source was incorrectly delivered")


def _verify_reply(
    *, cell: Mapping[str, object], result: Mapping[str, object], evidence: Mapping[str, object]
) -> None:
    external_send = result.get("external_send_performed")
    if not isinstance(external_send, bool):
        raise EvidenceVerificationError(
            "reply result.external_send_performed must be an explicit boolean"
        )
    trace = _trace_for(evidence, result)
    if trace.get("verified") is not True or trace.get("trace_kind") != "reply_delivery":
        raise EvidenceVerificationError("reply audit trace is not verified")
    draft = _mapping(trace.get("draft"), label="reply trace draft")
    approval = _mapping(trace.get("approval"), label="reply trace approval")
    delivery = _mapping(trace.get("delivery"), label="reply trace delivery")
    if result.get("draft_id") != draft.get("id"):
        raise EvidenceVerificationError("reply draft ID does not match audit trace")
    if result.get("channel") != cell.get("channel") or draft.get("channel") not in {
        None,
        cell.get("channel"),
    }:
        raise EvidenceVerificationError("reply audit trace channel does not match matrix cell")
    if result.get("approval_id") != approval.get("id"):
        raise EvidenceVerificationError("approval ID does not match audit trace")
    if delivery.get("kind") != "send-record@1":
        raise EvidenceVerificationError("reply delivery is not send-record@1")
    if result.get("send_record_id") != delivery.get("id"):
        raise EvidenceVerificationError("send record ID does not match audit trace")
    if external_send is True and not _required_bool(
        evidence, key="allow_external_send", label="evidence"
    ):
        raise EvidenceVerificationError(
            "external send evidence requires explicit allow_external_send"
        )


def verify_results(
    results: Mapping[str, object], evidence: Mapping[str, object]
) -> dict[str, object]:
    """Verify all 26 applicable results against Ledger/audit-trace evidence.

    The verifier does not create events and does not contact a channel.  A
    caller may first capture `/api/events`, notification projections, and the
    existing audit-trace responses into the evidence document.
    """

    if results.get("matrix_version") != MATRIX_VERSION:
        raise EvidenceVerificationError("results matrix_version does not match ACR-08")
    cells = {str(cell["cell_id"]): cell for cell in build_matrix()}
    applicable = {
        cell_id for cell_id, cell in cells.items() if bool(cell["applicable"])
    }
    raw_results = results.get("cells")
    if not isinstance(raw_results, list):
        raise EvidenceVerificationError("results.cells must be an array")
    real_external_sends = _required_bool(
        results, key="real_external_sends_performed", label="results"
    )
    allow_external_send = _required_bool(
        evidence, key="allow_external_send", label="evidence"
    )
    result_by_id: dict[str, Mapping[str, object]] = {}
    for raw in raw_results:
        item = _mapping(raw, label="result cell")
        cell_id = item.get("cell_id")
        if not isinstance(cell_id, str) or cell_id in result_by_id:
            raise EvidenceVerificationError("results contain a missing or duplicate cell_id")
        result_by_id[cell_id] = item
    if set(result_by_id) != applicable:
        missing = sorted(applicable - set(result_by_id))
        extra = sorted(set(result_by_id) - applicable)
        raise EvidenceVerificationError(
            f"results must contain exactly the 26 applicable cells; missing={missing}, extra={extra}"
        )
    if real_external_sends and not allow_external_send:
        raise EvidenceVerificationError(
            "real external sends are forbidden unless evidence explicitly opts in"
        )

    verified: list[str] = []
    for cell_id, result in result_by_id.items():
        if result.get("status") != "passed":
            raise EvidenceVerificationError(f"cell {cell_id} is not passed")
        cell = cells[cell_id]
        if result.get("channel") != cell["channel"]:
            raise EvidenceVerificationError(f"cell {cell_id} has no matching channel")
        if cell["direction"] == "agent_to_owner":
            _verify_reply(cell=cell, result=result, evidence=evidence)
        elif cell["resolution"] == "observation_only":
            _verify_observation_only(result=result, evidence=evidence)
        else:
            _verify_notification(cell=cell, result=result, evidence=evidence)
        verified.append(cell_id)
    return {
        "schema": "nanihold/acr08-verification",
        "matrix_version": MATRIX_VERSION,
        "verified": True,
        "verified_count": len(verified),
        "na_count": len(cells) - len(verified),
        "real_external_sends_performed": real_external_sends,
        "cell_ids": verified,
    }


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"JSONを読み込めません: {path}: {exc}") from exc
    return _mapping(value, label=str(path))  # type: ignore[return-value]


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACR-08 matrix and dry-run verifier")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("matrix", "checklist", "dry-run"):
        command = subparsers.add_parser(name)
        command.add_argument("--output", type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--results", type=Path, required=True)
    verify.add_argument("--evidence", type=Path, required=True)
    verify.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "matrix":
        value: object = matrix_manifest()
    elif args.command == "checklist":
        value = {"schema": "nanihold/acr08-owner-checklist", "markdown": render_owner_checklist()}
    elif args.command == "dry-run":
        value = automation_plan()
    else:
        value = verify_results(_read_json(args.results), _read_json(args.evidence))

    if args.output is not None:
        if isinstance(value, str):
            args.output.write_text(value, encoding="utf-8")
        else:
            _write_json(args.output, _mapping(value, label="command output"))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


__all__ = [
    "CHANNELS",
    "DATA_SPACE_ID",
    "DIRECTIONS",
    "EvidenceVerificationError",
    "MATRIX_VERSION",
    "RECIPIENT_KINDS",
    "RESOLUTION_MODES",
    "automation_plan",
    "build_matrix",
    "main",
    "matrix_manifest",
    "render_matrix_markdown",
    "render_owner_checklist",
    "verify_results",
]


if __name__ == "__main__":
    raise SystemExit(main())
