"""Human-readable projection of low-level VSM events."""

from __future__ import annotations

from typing import Any

STAGE_BY_EVENT = {
    "task_submitted": ("受付", 5),
    "s4_assessment_produced": ("環境分析", 25),
    "policy_decision": ("方針決定", 45),
    "s1_instantiated": ("実行チーム編成", 55),
    "s1_assignment_sent": ("作業割当", 65),
    "s1_completion": ("作業実行", 82),
    "audit_finding": ("監査", 92),
    "run_completed": ("完了", 100),
}

VISIBLE_EVENT_TYPES = {
    "task_submitted",
    "s4_assessment_produced",
    "policy_decision",
    "s1_instantiated",
    "s1_assignment_sent",
    "s1_completion",
    "audit_observation",
    "audit_finding",
    "audit_report_sent",
    "llm_timeout",
    "llm_error",
    "delivery_error",
    "dispatch_error",
}


def project_event(event: dict[str, Any], generation: int, superseded: bool = False) -> dict[str, Any] | None:
    event_type = event.get("event_type", "")
    if event_type not in VISIBLE_EVENT_TYPES:
        return None
    payload = event.get("payload") or {}
    stage, progress = STAGE_BY_EVENT.get(event_type, ("処理", 0))
    return {
        "id": event.get("event_id") or f"{generation}:{event.get('seq', 0)}",
        "generation": generation,
        "seq": event.get("seq", 0),
        "ts": event.get("ts"),
        "type": event_type,
        "stage": stage,
        "progress": progress,
        "system": _system_name(event_type, payload),
        "title": _title(event_type),
        "summary": _summary(event_type, payload),
        "details": _details(event_type, payload),
        "superseded": superseded,
    }


def _system_name(event_type: str, payload: dict[str, Any]) -> str:
    if event_type.startswith("s4_"):
        return "S4 Scanner"
    if event_type == "policy_decision":
        return "S5 Policy"
    if event_type.startswith("s1_"):
        return "S1 Worker"
    if event_type.startswith("audit_"):
        return "S3* Auditor"
    if event_type in {"llm_error", "llm_timeout"}:
        return payload.get("system_id", "LLM")
    return "Nanihold OS"


def _title(event_type: str) -> str:
    return {
        "task_submitted": "タスクを受け付けました",
        "s4_assessment_produced": "環境と論点を整理しました",
        "policy_decision": "実行方針を決定しました",
        "s1_instantiated": "実行担当を編成しました",
        "s1_assignment_sent": "作業を割り当てました",
        "s1_completion": "担当作業が完了しました",
        "audit_observation": "実行状態を確認しました",
        "audit_finding": "監査結果をまとめました",
        "audit_report_sent": "監査結果を方針層へ共有しました",
        "llm_timeout": "モデル応答がタイムアウトしました",
        "llm_error": "モデル呼び出しでエラーが発生しました",
        "delivery_error": "メッセージ配送を再試行しています",
        "dispatch_error": "指示の配送に失敗しました",
    }.get(event_type, event_type)


def _summary(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "task_submitted":
        return payload.get("description", "")
    if event_type == "s4_assessment_produced":
        opportunities = payload.get("opportunities") or []
        threats = payload.get("threats") or []
        return f"機会 {len(opportunities)}件、注意点 {len(threats)}件を抽出しました。"
    if event_type == "policy_decision":
        return payload.get("directive", "")
    if event_type == "s1_instantiated":
        return f"{payload.get('specialization', '担当')}の実行担当を起動しました。"
    if event_type == "s1_assignment_sent":
        return "決定した方針に基づいて具体的な作業を開始しました。"
    if event_type == "s1_completion":
        result = payload.get("result") or {}
        return result.get("text", "作業が完了しました。")
    if event_type == "audit_observation":
        return "作業状況と完了内容を監査しています。"
    if event_type == "audit_finding":
        return payload.get("content", "")
    if event_type == "audit_report_sent":
        return "監査結果の共有が完了しました。"
    if event_type in {"llm_timeout", "llm_error", "delivery_error", "dispatch_error"}:
        return payload.get("provider_message") or payload.get("reason") or payload.get("error") or "処理を継続できませんでした。"
    return ""


def _details(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    hidden = {"prompt", "response"}
    return {key: value for key, value in payload.items() if key not in hidden}

