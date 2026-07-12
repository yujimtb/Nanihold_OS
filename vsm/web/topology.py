"""Event_Log だけから再構成する Web 組織図・予算 projection。"""

from __future__ import annotations

from typing import Any


_STATUS_EVENTS = {
    "node_created": "CREATED",
    "node_started": "RUNNING",
    "node_idled": "IDLE",
    "node_suspended": "SUSPENDED",
    "node_resumed": "RUNNING",
    "node_completed": "COMPLETED",
    "node_terminated": "TERMINATED",
    "node_failed": "FAILED",
}


def _short(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _event_summary(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "tool_invoked":
        return f"{payload.get('tool_name', 'tool')} を実行"
    if event_type == "llm_invocation":
        return _short(payload.get("response") or payload.get("prompt") or "モデルを呼び出し")
    if event_type == "instruction_received":
        return f"指示を受信: {_short(payload.get('instruction'))}"
    if event_type == "policy_decision":
        return f"方針: {_short(payload.get('directive'))}"
    if event_type == "coordination_directive":
        return f"調整指示: {_short(payload.get('directive'))}"
    if event_type == "agent_attached":
        backend = payload.get("backend", "未接続")
        model = payload.get("model", "")
        return f"{backend}{f' / {model}' if model else ''} に接続"
    if event_type == "budget_consumed":
        return "予算を消費"
    if event_type.startswith("node_"):
        return f"状態: {payload.get('status') or event_type.removeprefix('node_')}"
    return event_type.replace("_", " ")


def _record_event(node: dict[str, Any], event: dict[str, Any], summary: str) -> None:
    node["recent_events"].insert(
        0,
        {
            "event_id": event.get("event_id"),
            "seq": event.get("seq"),
            "ts": event.get("ts"),
            "event_type": event.get("event_type", ""),
            "summary": summary,
            "actor_type": event.get("actor_type"),
            "actor_id": event.get("actor_id"),
        },
    )
    del node["recent_events"][8:]


def project_topology(events: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    consortium_conveners: dict[str, str] = {}
    consortium_subjects: dict[str, str] = {}
    waiting_consortiums: dict[str, dict[str, Any]] = {}
    pending_reviews: dict[str, dict[str, Any]] = {}

    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        node_id = payload.get("node_id") or event.get("node_id")
        if event_type == "node_created":
            node_id = payload["node_id"]
            nodes[node_id] = {
                "node_id": node_id,
                "parent_id": payload.get("parent_id"),
                "role": payload.get("vsm_position", ""),
                "status": "CREATED",
                "terminable": bool(payload.get("terminable", True)),
                "backend": "",
                "model": "",
                "activity": "起動準備",
                "last_activity_at": None,
                "recent_events": [],
                "authority": {"kind": "system", "summary": "Run 初期構成"},
                "budget": {
                    "tokens_limit": 0.0,
                    "tokens_consumed": 0.0,
                    "wall_clock_seconds_limit": 0.0,
                    "wall_clock_seconds_consumed": 0.0,
                },
            }
            _record_event(nodes[node_id], event, _event_summary(event_type, payload))
            continue
        if event_type in _STATUS_EVENTS and node_id in nodes:
            status = payload.get("status", _STATUS_EVENTS[event_type])
            nodes[node_id]["status"] = (
                "WAITING" if status == "WAITING_ESCALATION" else status
            )
            _record_event(nodes[node_id], event, _event_summary(event_type, payload))
            continue
        if event_type == "agent_attached" and node_id in nodes:
            node = nodes[node_id]
            node["backend"] = payload.get("backend", "")
            node["model"] = payload.get("model", "")
            budget = payload.get("budget") or {}
            node["budget"]["tokens_limit"] = float(budget.get("tokens", 0))
            node["budget"]["wall_clock_seconds_limit"] = float(
                budget.get("wall_clock_seconds", 0)
            )
            _record_event(node, event, _event_summary(event_type, payload))
            continue
        if event_type in {"tool_invoked", "llm_invocation"} and node_id in nodes:
            if event_type == "tool_invoked":
                nodes[node_id]["activity"] = f"{payload.get('tool_name', 'tool')} を実行中"
            else:
                nodes[node_id]["activity"] = _short(
                    payload.get("response") or payload.get("prompt") or "モデルを呼び出し中"
                )
                nodes[node_id]["backend"] = payload.get("backend", nodes[node_id]["backend"])
                nodes[node_id]["model"] = payload.get("model", nodes[node_id]["model"])
            nodes[node_id]["last_activity_at"] = event.get("ts")
            _record_event(nodes[node_id], event, _event_summary(event_type, payload))
            continue
        if event_type == "budget_consumed" and node_id in nodes:
            cumulative = payload.get("cumulative") or {}
            nodes[node_id]["budget"]["tokens_consumed"] = float(
                cumulative.get("tokens_total", 0)
            )
            nodes[node_id]["budget"]["wall_clock_seconds_consumed"] = float(
                cumulative.get("wall_clock_ms", 0)
            ) / 1000
            _record_event(nodes[node_id], event, _event_summary(event_type, payload))
            continue
        if event_type == "instruction_received":
            target = payload.get("target_node")
            if target in nodes:
                nodes[target]["authority"] = {
                    "kind": "instruction",
                    "id": payload.get("instruction_id"),
                    "summary": _short(payload.get("instruction")),
                    "source": event.get("actor_id") or event.get("actor_type") or "human",
                }
                _record_event(nodes[target], event, _event_summary(event_type, payload))
            continue
        if event_type in {"policy_decision", "coordination_directive"}:
            target = payload.get("target_node") or payload.get("receiver_id")
            if target in nodes:
                nodes[target]["authority"] = {
                    "kind": "decision" if event_type == "policy_decision" else "directive",
                    "id": payload.get("decision_id") or payload.get("directive_id"),
                    "summary": _short(payload.get("directive")),
                    "source": event.get("actor_id") or event.get("actor_type"),
                }
                _record_event(nodes[target], event, _event_summary(event_type, payload))
            continue
        if event_type == "consortium_convened":
            consortium_id = payload.get("consortium_id", "")
            consortium_conveners[consortium_id] = payload.get(
                "convener", payload.get("convener_node_id", "")
            )
            consortium_subjects[consortium_id] = payload.get("subject", "")
            continue
        if event_type == "consortium_waiting":
            consortium_id = payload.get("consortium_id", "")
            convener = consortium_conveners.get(consortium_id)
            if convener in nodes:
                nodes[convener]["status"] = "WAITING"
                _record_event(nodes[convener], event, f"合議体で判断待ち: {_short(consortium_subjects.get(consortium_id))}")
            waiting_consortiums[consortium_id] = {
                **payload,
                "subject": consortium_subjects.get(consortium_id, ""),
            }
            continue
        if event_type in {"consortium_decided", "consortium_aborted"}:
            consortium_id = payload.get("consortium_id", "")
            convener = consortium_conveners.get(consortium_id)
            if convener in nodes and nodes[convener]["status"] == "WAITING":
                nodes[convener]["status"] = "RUNNING"
            waiting_consortiums.pop(consortium_id, None)
            continue
        if event_type == "human_review_requested":
            key = payload.get("review_key")
            if key:
                pending_reviews[key] = payload
            continue
        if event_type == "human_review_responded":
            pending_reviews.pop(payload.get("review_key"), None)

    ordered = sorted(nodes.values(), key=lambda item: (item["parent_id"] or "", item["role"], item["node_id"]))
    return {
        "run_id": run_id,
        "nodes": ordered,
        "pending_human_reviews": list(pending_reviews.values()),
        "waiting_consortiums": list(waiting_consortiums.values()),
    }


def project_budget(events: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    configured = next(
        ((event.get("payload") or {}) for event in events if event.get("event_type") == "budget_configured"),
        {},
    )
    node_totals: dict[str, dict[str, float]] = {}
    run_cumulative: dict[str, float] = {
        "tokens_in": 0.0,
        "tokens_out": 0.0,
        "tokens_cache_read": 0.0,
        "tokens_total": 0.0,
        "wall_clock_ms": 0.0,
    }
    for event in events:
        if event.get("event_type") != "budget_consumed":
            continue
        payload = event.get("payload") or {}
        node_id = payload.get("node_id") or event.get("node_id")
        if node_id:
            node_totals[node_id] = {
                key: float(value) for key, value in (payload.get("cumulative") or {}).items()
            }
        run_cumulative = {
            key: float(value) for key, value in (payload.get("run_cumulative") or run_cumulative).items()
        }
    return {
        "run_id": run_id,
        "limit": {
            "tokens": float(configured.get("run_tokens", 0)),
            "wall_clock_seconds": float(configured.get("run_wall_clock_seconds", 0)),
        },
        "consumed": {
            **run_cumulative,
            "wall_clock_seconds": run_cumulative.get("wall_clock_ms", 0) / 1000,
        },
        "nodes": node_totals,
    }
