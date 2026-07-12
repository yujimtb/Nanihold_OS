"""Event-backed store and JSON projection cache for web runs."""

from __future__ import annotations

import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vsm.eventlog.schema import Event, validate_event_payload
from vsm.web.models import Attachment, RunGeneration, WebRun, WebRunStatus


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._next_seq: dict[str, int] = {}

    def save(self, run: WebRun) -> None:
        """Write the disposable current-state projection cache."""
        run.run_dir.mkdir(parents=True, exist_ok=True)
        self._save_attachments(run)
        payload = {
            "run_id": run.run_id,
            "title": run.title,
            "description": run.description,
            "constraints": run.constraints,
            "budget_override": run.budget_override,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "status": run.status.value,
            "final_answer": run.final_answer,
            "error": run.error,
            "current_stage": run.current_stage,
            "progress": run.progress,
            "pending_instruction": run.pending_instruction,
            "attachments": [
                {
                    **attachment.public_dict(),
                    "path": str(attachment.path),
                    "extracted_text": attachment.extracted_text,
                    "model_content": attachment.model_content,
                }
                for attachment in run.attachments
            ],
            "generations": [generation.__dict__ for generation in run.generations],
        }
        (run.run_dir / "run.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def create(self, run: WebRun) -> None:
        self._save_input(run)
        self._save_attachments(run)
        self.append_event(
            run,
            "web_run_created",
            {
                "title": run.title,
                "description_ref": "input.json",
                "constraints": run.constraints,
                "budget_override": run.budget_override,
                "created_at": run.created_at,
                "updated_at": run.updated_at,
                "status": run.status.value,
                "current_stage": run.current_stage,
                "progress": run.progress,
            },
            actor_type="human",
            actor_id="local-user",
        )
        self.save(run)

    def record_state(self, run: WebRun, reason: str) -> None:
        self.append_event(
            run,
            "web_run_state_changed",
            {
                "status": run.status.value,
                "current_stage": run.current_stage,
                "progress": run.progress,
                "error": run.error,
                "pending_instruction": run.pending_instruction,
                "updated_at": run.updated_at,
                "reason": reason,
            },
        )
        self.save(run)

    def append_event(
        self,
        run: WebRun,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor_type: str = "system",
        actor_id: str | None = "web-runtime",
    ) -> dict[str, Any]:
        validate_event_payload(event_type, payload)
        run.run_dir.mkdir(parents=True, exist_ok=True)
        path = self.events_path(run)
        with self._lock:
            seq = self._next_seq.setdefault(run.run_id, self._read_next_seq(path))
            event = Event(
                seq=seq,
                run_id=run.run_id,
                stream_id=f"web-run:{run.run_id}",
                stream_version=seq + 1,
                event_type=event_type,
                ts=_utc_now(),
                actor_type=actor_type,
                actor_id=actor_id,
                correlation_id=run.run_id,
                payload=payload,
            )
            with path.open("a", encoding="utf-8", buffering=1) as fh:
                fh.write(event.model_dump_json() + "\n")
                fh.flush()
            self._next_seq[run.run_id] = seq + 1
        return event.model_dump()

    def load(self, run_id: str) -> WebRun:
        path = self.root / run_id / "run.json"
        events_path = path.parent / "events.jsonl"
        if events_path.exists():
            return self._project(run_id, events_path)
        run = self._load_legacy(path)
        self._migrate_legacy(run)
        return run

    def _load_legacy(self, path: Path) -> WebRun:
        payload = json.loads(path.read_text(encoding="utf-8"))
        attachments = self._attachments_from_payload(payload.get("attachments", []))
        return WebRun(
            run_id=payload["run_id"],
            title=payload["title"],
            description=payload["description"],
            constraints=payload.get("constraints", {}),
            budget_override=payload.get("budget_override", {}),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            status=WebRunStatus(payload["status"]),
            run_dir=path.parent,
            attachments=attachments,
            generations=[RunGeneration(**item) for item in payload.get("generations", [])],
            final_answer=payload.get("final_answer"),
            error=payload.get("error"),
            current_stage=payload.get("current_stage", "queued"),
            progress=payload.get("progress", 0),
            pending_instruction=payload.get("pending_instruction"),
        )

    def _project(self, run_id: str, events_path: Path) -> WebRun:
        events = self.read_events_path(events_path)
        created = next(
            event for event in events if event["event_type"] == "web_run_created"
        )
        payload = created["payload"]
        run_dir = events_path.parent
        input_payload = self._load_input(run_dir)
        if not input_payload and "description" in payload:
            input_payload = {"description": payload["description"]}
            self._write_input_payload(run_dir, input_payload)
        run = WebRun(
            run_id=run_id,
            title=payload["title"],
            description=input_payload.get(
                "description",
                payload.get("description", ""),
            ),
            constraints=input_payload.get("constraints", payload.get("constraints", {})),
            budget_override=input_payload.get(
                "budget_override", payload.get("budget_override", {})
            ),
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            status=WebRunStatus(payload["status"]),
            run_dir=run_dir,
            attachments=self._load_attachments(run_dir),
            current_stage=payload["current_stage"],
            progress=payload["progress"],
        )
        for event in events:
            self._apply(run, event)
        answer_path = run_dir / "artifacts" / "final-answer.md"
        if answer_path.exists():
            run.final_answer = answer_path.read_text(encoding="utf-8")
        return run

    @staticmethod
    def _apply(run: WebRun, event: dict[str, Any]) -> None:
        payload = event["payload"]
        event_type = event["event_type"]
        if event_type == "web_run_state_changed":
            run.status = WebRunStatus(payload["status"])
            run.current_stage = payload["current_stage"]
            run.progress = payload["progress"]
            run.error = payload.get("error")
            run.pending_instruction = payload.get("pending_instruction")
            run.updated_at = payload["updated_at"]
        elif event_type == "web_generation_started":
            run.generations.append(
                RunGeneration(
                    generation=payload["generation"],
                    runtime_run_id=payload["runtime_run_id"],
                    instruction=payload.get("instruction", ""),
                    started_at=payload["started_at"],
                )
            )
        elif event_type == "web_generation_finished":
            generation = next(
                (
                    item
                    for item in run.generations
                    if item.generation == payload["generation"]
                ),
                None,
            )
            if generation is not None:
                generation.status = payload["status"]
                generation.finished_at = payload["finished_at"]
        elif event_type == "web_instruction_received":
            run.pending_instruction = payload["instruction"]
        elif event_type == "web_run_renamed":
            run.title = payload["title"]
            run.updated_at = payload["updated_at"]

    def list(self) -> list[WebRun]:
        runs = []
        run_dirs = {
            path.parent for path in self.root.glob("*/run.json")
        } | {
            path.parent for path in self.root.glob("*/events.jsonl")
        }
        for run_dir in run_dirs:
            try:
                runs.append(self.load(run_dir.name))
            except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError):
                continue
        return sorted(runs, key=lambda run: run.updated_at, reverse=True)

    def delete(self, run_id: str) -> None:
        path = self.root / run_id
        if path.exists():
            shutil.rmtree(path)

    def read_events(self, run: WebRun) -> list[dict[str, Any]]:
        path = self.events_path(run)
        return self.read_events_path(path) if path.exists() else []

    @staticmethod
    def read_events_path(path: Path) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def events_path(run: WebRun) -> Path:
        return run.run_dir / "events.jsonl"

    @staticmethod
    def _read_next_seq(path: Path) -> int:
        if not path.exists():
            return 0
        events = RunStore.read_events_path(path)
        return max((event["seq"] for event in events), default=-1) + 1

    def _migrate_legacy(self, run: WebRun) -> None:
        self.create(run)
        for generation in run.generations:
            self.append_event(
                run,
                "web_generation_started",
                {
                    "generation": generation.generation,
                    "runtime_run_id": generation.runtime_run_id,
                    "instruction": generation.instruction,
                    "started_at": generation.started_at,
                },
            )
            if generation.finished_at:
                self.append_event(
                    run,
                    "web_generation_finished",
                    {
                        "generation": generation.generation,
                        "status": generation.status,
                        "finished_at": generation.finished_at,
                    },
                )
        self._migrate_legacy_controls(run)
        self.record_state(run, "legacy_projection_migrated")

    def _migrate_legacy_controls(self, run: WebRun) -> None:
        path = run.run_dir / "control-events.jsonl"
        if not path.exists():
            return
        event_types = {
            "instruction_received": "web_instruction_received",
            "automatic_retry": "web_retry_started",
            "retry_started": "web_retry_started",
            "run_cancelled": "web_run_cancelled",
            "run_completed": "web_run_completed",
            "partial_result_accepted": "web_partial_result_accepted",
        }
        for legacy in self.read_events_path(path):
            legacy_type = legacy.get("type")
            if legacy_type not in event_types:
                continue
            payload = {
                "generation": legacy.get("generation", run.generation),
                **legacy.get("payload", {}),
            }
            if legacy_type == "run_completed":
                payload.pop("final_answer", None)
                payload["answer_ref"] = "artifacts/final-answer.md"
            self.append_event(
                run,
                event_types[legacy_type],
                payload,
                actor_type=(
                    "human"
                    if legacy_type
                    in {
                        "instruction_received",
                        "retry_started",
                        "run_cancelled",
                        "partial_result_accepted",
                    }
                    else "system"
                ),
                actor_id=(
                    "local-user"
                    if legacy_type
                    in {
                        "instruction_received",
                        "retry_started",
                        "run_cancelled",
                        "partial_result_accepted",
                    }
                    else "web-runtime"
                ),
            )

    def _save_input(self, run: WebRun) -> None:
        self._write_input_payload(
            run.run_dir,
            {
                "description": run.description,
                "constraints": run.constraints,
                "budget_override": run.budget_override,
            },
        )

    @staticmethod
    def _write_input_payload(run_dir: Path, payload: dict[str, Any]) -> None:
        path = run_dir / "input.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _load_input(run_dir: Path) -> dict[str, Any]:
        path = run_dir / "input.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_attachments(self, run: WebRun) -> None:
        if not run.attachments:
            return
        path = run.run_dir / "attachments.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                [
                    {
                        "attachment_id": item.attachment_id,
                        "name": item.name,
                        "media_type": item.media_type,
                        "size": item.size,
                        "path": str(item.path),
                        "extracted_text": item.extracted_text,
                        "model_content": item.model_content,
                    }
                    for item in run.attachments
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _load_attachments(self, run_dir: Path) -> list[Attachment]:
        path = run_dir / "attachments.json"
        if not path.exists():
            return []
        return self._attachments_from_payload(
            json.loads(path.read_text(encoding="utf-8"))
        )

    @staticmethod
    def _attachments_from_payload(items: list[dict[str, Any]]) -> list[Attachment]:
        return [
            Attachment(
                attachment_id=item["attachment_id"],
                name=item["name"],
                media_type=item["media_type"],
                size=item["size"],
                path=Path(item["path"]),
                extracted_text=item.get("extracted_text", ""),
                model_content=item.get("model_content"),
            )
            for item in items
        ]
