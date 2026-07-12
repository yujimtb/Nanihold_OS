"""自己開発 Proposal 専用の REST surface と projection。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from vsm.clock import format_iso_ms
from vsm.ids import generate_uuid
from vsm.selfdev.controller import ControllerError, ControllerPaused
from vsm.selfdev.models import ProposalManifest
from vsm.selfdev.state_machine import ProposalPhase, PauseKind
from vsm.selfdev.verification import scope_sha256
from vsm.web.selfdev_models import (
    HumanDecisionBody,
    MergeOutcomeBody,
    ProposalControlBody,
    ProposalCreateBody,
)

router = APIRouter(prefix="/api/selfdev", tags=["selfdev"])


def _service(request: Request) -> Any:
    service = getattr(request.app.state, "selfdev_service", None)
    if service is None:
        # 既存の app テストは module global を monkeypatch するため、
        # app factory を使わない注入経路も同じ controller を参照する。
        import sys

        module = sys.modules.get("vsm.web.app")
        service = getattr(module, "selfdev_service", None) if module is not None else None
    if service is None:
        startup_error = getattr(request.app.state, "selfdev_startup_error", None)
        detail = "selfdev controller は配備されていません"
        if startup_error is not None:
            detail = f"selfdev controller の配備に失敗しました: {startup_error}"
        raise HTTPException(status_code=503, detail=detail)
    controller = getattr(service, "controller", None)
    healthy = getattr(service, "healthy", False)
    if controller is None or not healthy:
        fatal = getattr(service, "fatal", None)
        detail = "selfdev controller は degraded 停止中です"
        if fatal:
            detail = f"{detail}: {fatal}"
        raise HTTPException(status_code=503, detail=detail)
    return service


def _controller(request: Request) -> Any:
    return _service(request).controller


def _store(controller: Any) -> Any:
    store = getattr(controller, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="selfdev store が配備されていません")
    return store


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    return dict(event)


def _events(controller: Any, proposal_id: str) -> list[Any]:
    try:
        return [
            event
            for event in _store(controller).read_events()
            if event.payload.get("proposal_id") == proposal_id
        ]
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="selfdev Event Log がありません") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"selfdev Event Log の読込に失敗しました: {exc}") from exc


def _manifest(controller: Any, proposal_id: str) -> ProposalManifest:
    layout = _store(controller).layout
    path = layout.proposal_manifest_path(proposal_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Proposal が見つかりません")
    try:
        return ProposalManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"ProposalManifest が不正です: {exc}") from exc


def _projection(controller: Any, proposal_id: str) -> Any:
    try:
        projection = _store(controller).projection(proposal_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Proposal projection の復元に失敗しました: {exc}") from exc
    if projection is None:
        raise HTTPException(status_code=404, detail="Proposal が見つかりません")
    return projection


def _pause_dict(cause: Any) -> dict[str, Any]:
    reset_at = cause.reset_at
    return {
        "pause_id": cause.pause_id,
        "kind": cause.kind.value,
        "actor_type": cause.actor_type,
        "actor_id": cause.actor_id,
        "pool_id": cause.pool_id,
        "reset_at": format_iso_ms(reset_at) if reset_at is not None else None,
        "source_event_id": cause.source_event_id,
        "reason": cause.reason,
    }


def _pending_action(projection: Any, manifest: ProposalManifest) -> str | None:
    phase = projection.aggregate.phase
    if phase is ProposalPhase.NEEDS_HUMAN:
        return "protected_scope_approval" if manifest.risk_class == "protected" else "human_decision"
    if phase is ProposalPhase.MERGE_READY:
        return "merge_outcome"
    if projection.aggregate.pause_causes:
        return "resume"
    return None


def _latest_timestamp(events: list[Any]) -> str:
    if not events:
        raise HTTPException(status_code=503, detail="Proposal に Event Log がありません")
    return max(str(event.ts) for event in events)


def _artifact_records(events: list[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "artifact_created":
            continue
        payload = dict(event.payload)
        records.append(
            {
                "name": payload["ref"],
                "kind": payload.get("artifact_kind", "artifact"),
                "sha256": payload["sha256"],
                "event_id": event.event_id,
                "created_at": event.ts,
            }
        )
    return records


def _read_json_if_exists(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail=f"artifact の読込に失敗しました: {path}") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=503, detail=f"artifact は object でなければなりません: {path}")
    return value


def _detail(controller: Any, proposal_id: str) -> dict[str, Any]:
    manifest = _manifest(controller, proposal_id)
    projection = _projection(controller, proposal_id)
    events = _events(controller, proposal_id)
    layout = _store(controller).layout
    state_events = [event for event in events if event.event_type == "proposal_state_changed"]
    transition_payloads = []
    for event in state_events:
        item = _event_dict(event)
        item["transition"] = dict(event.payload)
        transition_payloads.append(item)

    gate_attempts: list[dict[str, Any]] = []
    for event in events:
        if event.event_type != "gate_report_generated":
            continue
        item = _event_dict(event)
        ref = event.payload.get("report_ref")
        if isinstance(ref, str):
            report = _read_json_if_exists(layout.proposal_dir(proposal_id) / ref)
            if report is not None:
                item["report"] = report
        gate_attempts.append(item)

    candidate = _read_json_if_exists(layout.artifacts_dir(proposal_id) / "candidate-commit.json")
    if candidate is not None:
        candidate = {
            "branch": candidate.get("branch"),
            "commit_sha": candidate.get("commit_sha"),
            "base_sha": candidate.get("base_sha"),
            "diff_sha256": candidate.get("diff_sha256"),
        }
    audit_report = _read_json_if_exists(layout.audit_dir(proposal_id) / "audit_report.json")
    pr_path = layout.pr_description_path(proposal_id)
    try:
        pr_description = pr_path.read_text(encoding="utf-8") if pr_path.is_file() else None
    except OSError as exc:
        raise HTTPException(status_code=503, detail="PR説明文を読めません") from exc

    errors = [
        event.payload.get("reason", "")
        for event in state_events
        if event.payload.get("to_state") in {"GATES_FAILED", "ABORTED", "REJECTED", "REJECTED_FINAL"}
    ]
    budget_actual = getattr(controller, "_budget_actual", {})
    if not isinstance(budget_actual, dict):
        budget_actual = {}

    return {
        "schema_version": 1,
        "proposal_id": manifest.id,
        "title": manifest.title,
        "risk_class": manifest.risk_class,
        "proposal": manifest.model_dump(mode="json"),
        "proposal_manifest_sha256": manifest.sha256(),
        "protected_scope_sha256": scope_sha256([rule.model_dump(mode="json") for rule in manifest.scope]),
        "state": projection.aggregate.phase.value,
        "pause_causes": [_pause_dict(cause) for cause in projection.aggregate.pause_causes],
        "state_version": projection.aggregate.state_version,
        "active_run_id": projection.aggregate.active_run_id,
        "pending_action": _pending_action(projection, manifest),
        "transitions": transition_payloads,
        "consortium_reviews":[
            _event_dict(event)
            for event in events
            if event.event_type == "consortium_decided"
        ],
        "implementation_runs": [dict(link) for link in projection.run_links],
        "gate_attempts": gate_attempts,
        "audit_report": audit_report,
        "budget_actual": dict(budget_actual),
        "artifacts": _artifact_records(events),
        "candidate": candidate,
        "pr_description": pr_description,
        "last_error": errors[-1] if errors else None,
    }


def _summary(controller: Any, proposal_id: str) -> dict[str, Any]:
    detail = _detail(controller, proposal_id)
    proposal = detail["proposal"]
    return {
        "proposal_id": proposal_id,
        "title": proposal["title"],
        "state": detail["state"],
        "pause_causes": detail["pause_causes"],
        "state_version": detail["state_version"],
        "risk_class": proposal["risk_class"],
        "active_run_id": detail["active_run_id"],
        "pending_action": detail["pending_action"],
        "updated_at": _latest_timestamp(_events(controller, proposal_id)),
    }


def _check_version(controller: Any, proposal_id: str, expected: int) -> Any:
    projection = _projection(controller, proposal_id)
    if projection.aggregate.state_version != expected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"state_version が古いです: expected={expected}, "
                f"current={projection.aggregate.state_version}"
            ),
        )
    return projection


def _map_mutation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Proposal が見つかりません")
    if isinstance(exc, (ControllerError, ControllerPaused, ValueError, RuntimeError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


@router.post("/proposals", status_code=201)
async def create_proposal(body: ProposalCreateBody, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    try:
        manifest = ProposalManifest(
            id=f"proposal-{generate_uuid()}",
            title=body.title,
            motivation=body.motivation,
            scope=body.scope,
            acceptance_criteria=body.acceptance_criteria,
            risk_class=body.risk_class,
            budget_estimate=body.budget_estimate,
            origin=body.origin,
            dependencies=body.dependencies,
            created_at=format_iso_ms(datetime.now(timezone.utc)),
            created_by={"actor_type": "human", "actor_id": "web-api"},
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        await controller.submit_proposal(manifest)
    except Exception as exc:
        raise _map_mutation_error(exc) from exc
    return {
        "proposal_id": manifest.id,
        "state": ProposalPhase.PROPOSED.value,
        "state_version": 1,
        "risk_class": manifest.risk_class,
        "created_at": manifest.created_at,
    }


@router.get("/proposals")
def list_proposals(
    request: Request,
    state: ProposalPhase | None = Query(default=None),
    pending_action: str | None = Query(default=None),
) -> dict[str, list[dict[str, Any]]]:
    if pending_action not in {None, "human"}:
        raise HTTPException(status_code=422, detail="pending_action は human のみ指定できます")
    controller = _controller(request)
    try:
        projections = _store(controller).replay()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Proposal projection の復元に失敗しました: {exc}") from exc
    items: list[dict[str, Any]] = []
    for proposal_id in sorted(projections):
        item = _summary(controller, proposal_id)
        if state is not None and item["state"] != state.value:
            continue
        if pending_action == "human" and item["state"] != ProposalPhase.NEEDS_HUMAN.value:
            continue
        items.append(item)
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return {"items": items}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str, request: Request) -> dict[str, Any]:
    return _detail(_controller(request), proposal_id)


@router.get("/proposals/{proposal_id}/events")
def stream_proposal_events(proposal_id: str, request: Request) -> StreamingResponse:
    controller = _controller(request)
    _manifest(controller, proposal_id)
    events = _events(controller, proposal_id)

    async def generate():
        for event in events:
            yield f"event: selfdev\ndata: {json.dumps(_event_dict(event), ensure_ascii=False)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/proposals/{proposal_id}/control", status_code=202)
async def control_proposal(proposal_id: str, body: ProposalControlBody, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    projection = _check_version(controller, proposal_id, body.expected_state_version)
    if projection.aggregate.is_terminal:
        raise HTTPException(status_code=409, detail="terminal Proposal は操作できません")
    try:
        if body.action == "suspend":
            await controller.suspend(proposal_id, reason=body.reason)
        elif body.action == "abort":
            await controller.abort(proposal_id, reason=body.reason)
        else:
            pauses = projection.aggregate.pause_causes
            if len(pauses) != 1:
                raise ControllerError("resume 対象の pause cause を一意に指定できません")
            pause = pauses[0]
            if pause.kind is PauseKind.SUSPEND:
                await controller.resume_suspend(proposal_id, pause_id=pause.pause_id)
            else:
                if pause.pool_id is None:
                    raise ControllerError("QUOTA_WAIT の pool_id がありません")
                await controller.resume_quota(proposal_id, pool_id=pause.pool_id)
    except Exception as exc:
        raise _map_mutation_error(exc) from exc
    return {"accepted": True, "proposal_id": proposal_id, "action": body.action}


@router.post("/proposals/{proposal_id}/human-decision", status_code=202)
async def human_decision(proposal_id: str, body: HumanDecisionBody, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    projection = _check_version(controller, proposal_id, body.expected_state_version)
    if projection.aggregate.phase is not ProposalPhase.NEEDS_HUMAN:
        raise HTTPException(status_code=409, detail="NEEDS_HUMAN 以外では Human decision を受け付けません")
    manifest = _manifest(controller, proposal_id)
    if body.decision == "approve" and manifest.risk_class != "protected":
        raise HTTPException(status_code=409, detail="approve が必要なのは protected Proposal だけです")
    if body.decision == "approve":
        if body.proposal_manifest_sha256 is None or body.protected_scope_sha256 is None:
            raise HTTPException(status_code=422, detail="protected approve には2つの hash が必要です")
        if body.proposal_manifest_sha256 != manifest.sha256():
            raise HTTPException(status_code=422, detail="ProposalManifest hash が一致しません")
        expected_scope = scope_sha256([rule.model_dump(mode="json") for rule in manifest.scope])
        if body.protected_scope_sha256 != expected_scope:
            raise HTTPException(status_code=422, detail="protected scope hash が一致しません")
    try:
        decision = "statement" if body.decision == "respond" else body.decision
        response = body.statement if body.decision == "respond" else body.reason
        await controller.respond_human(proposal_id, decision=decision, response=response or "")
    except Exception as exc:
        raise _map_mutation_error(exc) from exc
    return {"accepted": True, "proposal_id": proposal_id, "decision": body.decision}


@router.post("/proposals/{proposal_id}/merge-outcome", status_code=202)
async def merge_outcome(proposal_id: str, body: MergeOutcomeBody, request: Request) -> dict[str, Any]:
    controller = _controller(request)
    projection = _projection(controller, proposal_id)
    if projection.aggregate.phase is not ProposalPhase.MERGE_READY:
        raise HTTPException(status_code=409, detail="MERGE_READY 以外では outcome を記録できません")
    try:
        await controller.record_merge_outcome(
            proposal_id,
            merged=body.merged,
            reason=body.reason,
            merge_sha=body.merge_sha,
        )
    except Exception as exc:
        raise _map_mutation_error(exc) from exc
    return {"accepted": True, "proposal_id": proposal_id, "merged": body.merged}


@router.get("/proposals/{proposal_id}/artifacts/{name:path}")
def get_artifact(proposal_id: str, name: str, request: Request) -> FileResponse:
    controller = _controller(request)
    detail = _detail(controller, proposal_id)
    ref = unquote(name)
    if not ref or ref.startswith("/") or "\\" in ref or "\x00" in ref:
        raise HTTPException(status_code=404, detail="成果物が見つかりません")
    if any(part in {"", ".", ".."} for part in ref.split("/")):
        raise HTTPException(status_code=404, detail="成果物が見つかりません")
    allowed = {str(item["name"]) for item in detail["artifacts"]}
    if ref not in allowed:
        raise HTTPException(status_code=404, detail="成果物が見つかりません")
    path = _store(controller).layout.proposal_dir(proposal_id) / ref
    try:
        path.resolve(strict=False).relative_to(_store(controller).layout.proposal_dir(proposal_id).resolve(strict=False))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="成果物が見つかりません") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="成果物が見つかりません")
    media_type = "text/markdown" if path.suffix == ".md" else "application/json"
    if path.suffix == ".patch":
        media_type = "text/x-diff"
    return FileResponse(path, filename=path.name, media_type=media_type)


@router.get("/health")
def selfdev_health(request: Request) -> dict[str, Any]:
    service = getattr(request.app.state, "selfdev_service", None)
    if service is None:
        import sys

        module = sys.modules.get("vsm.web.app")
        service = getattr(module, "selfdev_service", None) if module is not None else None
    if service is None:
        startup_error = getattr(request.app.state, "selfdev_startup_error", None)
        return {
            "status": "degraded",
            "controller": "fatal" if startup_error else "unconfigured",
            "lease": "unavailable",
            "reconcile": "failed" if startup_error else "unavailable",
            "fatal": str(startup_error) if startup_error else None,
        }
    controller = getattr(service, "controller", None)
    fatal = getattr(service, "fatal", None)
    healthy = bool(getattr(service, "healthy", False))
    lease = "held" if controller is not None and getattr(controller, "_started", False) else "released"
    return {
        "status": "ok" if healthy else "degraded",
        "controller": "running" if healthy else ("fatal" if fatal else "stopped"),
        "lease": lease,
        "reconcile": "ok" if healthy else "unknown",
        "fatal": str(fatal) if fatal else None,
    }


__all__ = ["router"]
