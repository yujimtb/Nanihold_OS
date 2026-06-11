"""Disk-backed metadata store for web runs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from vsm.web.models import Attachment, RunGeneration, WebRun, WebRunStatus


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, run: WebRun) -> None:
        run.run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run.run_id,
            "title": run.title,
            "description": run.description,
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

    def load(self, run_id: str) -> WebRun:
        path = self.root / run_id / "run.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        attachments = [
            Attachment(
                attachment_id=item["attachment_id"],
                name=item["name"],
                media_type=item["media_type"],
                size=item["size"],
                path=Path(item["path"]),
                extracted_text=item.get("extracted_text", ""),
                model_content=item.get("model_content"),
            )
            for item in payload.get("attachments", [])
        ]
        return WebRun(
            run_id=payload["run_id"],
            title=payload["title"],
            description=payload["description"],
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

    def list(self) -> list[WebRun]:
        runs = []
        for path in self.root.glob("*/run.json"):
            try:
                runs.append(self.load(path.parent.name))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return sorted(runs, key=lambda run: run.updated_at, reverse=True)

    def delete(self, run_id: str) -> None:
        path = self.root / run_id
        if path.exists():
            shutil.rmtree(path)

    def append_control_event(self, run: WebRun, event: dict[str, Any]) -> None:
        path = run.run_dir / "control-events.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

