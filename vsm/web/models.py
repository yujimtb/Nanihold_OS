"""Web-facing models that remain independent from FastAPI transport details."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class WebRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTING = "interrupting"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Attachment:
    attachment_id: str
    name: str
    media_type: str
    size: int
    path: Path
    extracted_text: str = ""
    model_content: dict[str, Any] | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "name": self.name,
            "media_type": self.media_type,
            "size": self.size,
            "has_text": bool(self.extracted_text),
        }


@dataclass
class RunGeneration:
    generation: int
    runtime_run_id: str
    instruction: str
    started_at: str
    status: str = "active"
    finished_at: str | None = None


@dataclass
class WebRun:
    run_id: str
    title: str
    description: str
    created_at: str
    updated_at: str
    status: WebRunStatus
    run_dir: Path
    constraints: dict[str, Any] = field(default_factory=dict)
    budget_override: dict[str, float] = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)
    generations: list[RunGeneration] = field(default_factory=list)
    final_answer: str | None = None
    error: str | None = None
    current_stage: str = "queued"
    progress: int = 0
    pending_instruction: str | None = None

    @property
    def generation(self) -> int:
        return len(self.generations)

