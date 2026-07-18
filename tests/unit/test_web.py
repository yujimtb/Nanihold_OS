from __future__ import annotations

import asyncio
import json
import re

import pytest

from vsm.config import AgentsConfig, LLMConfig, ResidencyConfig, RunConfig
from vsm.roles import SystemRole
from vsm.web.manager import RunManager, utc_now
from vsm.web.models import RunGeneration, WebRun, WebRunStatus
from vsm.web.projection import project_event
from vsm.web.store import RunStore


def make_run(tmp_path) -> WebRun:
    return WebRun(
        run_id="run-web-test",
        title="日本語のテスト",
        description="市場調査を行ってください",
        created_at="2026-06-11T00:00:00Z",
        updated_at="2026-06-11T00:00:00Z",
        status=WebRunStatus.COMPLETED,
        run_dir=tmp_path / "run-web-test",
        generations=[
            RunGeneration(
                generation=1,
                runtime_run_id="runtime-test",
                instruction="",
                started_at="2026-06-11T00:00:00Z",
                status="completed",
                finished_at="2026-06-11T00:01:00Z",
            )
        ],
        final_answer="# 調査結果\n\n完了しました。",
        current_stage="完了",
        progress=100,
    )


def test_run_store_rebuilds_projection_from_events(tmp_path) -> None:
    store = RunStore(tmp_path)
    run = make_run(tmp_path)
    run.status = WebRunStatus.QUEUED
    run.current_stage = "受付待ち"
    run.progress = 0
    run.generations = []
    run.final_answer = None

    store.create(run)
    generation = RunGeneration(
        generation=1,
        runtime_run_id="runtime-test",
        instruction="",
        started_at="2026-06-11T00:00:00Z",
        status="completed",
        finished_at="2026-06-11T00:01:00Z",
    )
    run.generations.append(generation)
    store.append_event(
        run,
        "web_generation_started",
        {
            "generation": generation.generation,
            "runtime_run_id": generation.runtime_run_id,
            "instruction": generation.instruction,
            "started_at": generation.started_at,
        },
    )
    store.append_event(
        run,
        "web_generation_finished",
        {
            "generation": generation.generation,
            "status": generation.status,
            "finished_at": generation.finished_at,
        },
    )
    run.status = WebRunStatus.COMPLETED
    run.current_stage = "完了"
    run.progress = 100
    run.final_answer = "# 調査結果\n\n完了しました。"
    artifact_dir = run.run_dir / "artifacts"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "final-answer.md").write_text(
        run.final_answer,
        encoding="utf-8",
    )
    store.record_state(run, "completed")

    (run.run_dir / "run.json").write_text("{broken projection", encoding="utf-8")
    loaded = store.load(run.run_id)

    assert loaded.title == "日本語のテスト"
    assert loaded.description == "市場調査を行ってください"
    assert loaded.final_answer == "# 調査結果\n\n完了しました。"
    assert loaded.generations[0].status == "completed"


def test_run_store_writes_formal_event_envelopes_and_object_refs(tmp_path) -> None:
    store = RunStore(tmp_path)
    run = make_run(tmp_path)

    store.create(run)

    events = store.read_events(run)
    assert events[0]["event_type"] == "web_run_created"
    assert events[0]["stream_id"] == f"web-run:{run.run_id}"
    assert events[0]["stream_version"] == 1
    assert events[0]["correlation_id"] == run.run_id
    assert events[0]["payload"]["description_ref"] == "input.json"
    assert "description" not in events[0]["payload"]
    assert json.loads((run.run_dir / "input.json").read_text(encoding="utf-8")) == {
        "description": "市場調査を行ってください",
        "constraints": {},
        "budget_override": {},
    }


def test_manager_writes_downloadable_artifacts(tmp_path) -> None:
    manager = RunManager(tmp_path)
    run = make_run(tmp_path)
    manager.store.create(run)
    manager._runs[run.run_id] = run

    manager._write_artifacts(run)

    artifacts = manager.artifacts(run)
    assert [item["name"] for item in artifacts] == [
        "final-answer.md",
        "process-log.json",
    ]
    assert manager.artifact_path(run.run_id, "final-answer.md").read_text(
        encoding="utf-8"
    ).startswith("# 調査結果")
    assert json.loads(
        manager.artifact_path(run.run_id, "process-log.json").read_text(
            encoding="utf-8"
        )
    ) == []
    artifact_events = [
        event
        for event in manager.store.read_events(run)
        if event["event_type"] == "artifact_created"
    ]
    assert [event["payload"]["artifact_ref"] for event in artifact_events] == [
        "artifacts/final-answer.md",
        "artifacts/process-log.json",
    ]


def test_projection_hides_prompts_and_marks_superseded() -> None:
    projected = project_event(
        {
            "event_id": "event-1",
            "seq": 3,
            "ts": "2026-06-11T00:00:10Z",
            "event_type": "s1_completion",
            "payload": {
                "result": {"text": "担当結果"},
                "prompt": "内部プロンプト",
                "response": "内部応答",
            },
        },
        generation=1,
        superseded=True,
    )

    assert projected is not None
    assert projected["title"] == "担当作業が完了しました"
    assert projected["summary"] == "担当結果"
    assert projected["superseded"] is True
    assert "prompt" not in projected["details"]
    assert "response" not in projected["details"]


def test_web_timestamp_matches_event_schema() -> None:
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z",
        utc_now(),
    )


@pytest.mark.asyncio
async def test_manager_fails_before_generation_when_litellm_provider_is_missing(
    tmp_path, monkeypatch
) -> None:
    roles = {role: "fake" for role in SystemRole}
    roles[SystemRole.S5_POLICY] = "litellm"
    roles[SystemRole.S3_ALLOCATOR] = ""
    run_config = RunConfig(
        agents=AgentsConfig(default_backend="fake", roles=roles),
        residency=ResidencyConfig(native_runs_enabled=True),
    )
    monkeypatch.setattr(
        "vsm.web.manager.load_config",
        lambda _path=None: (LLMConfig(), run_config),
    )

    manager = RunManager(tmp_path)
    run = await manager.create_run(
        description="設定エラーを検出する",
        title=None,
        attachments=[],
    )

    assert run.status is WebRunStatus.FAILED
    assert run.generation == 0
    assert run.current_stage == "設定エラー"
    assert "S5_POLICY" in run.error
    assert "LITELLM_PROVIDER" in run.error
    assert not (run.run_dir / "runtime").exists()
    detail = manager.detail(run.run_id)
    assert detail["status"] == "failed"
    assert detail["error"] == run.error


@pytest.mark.asyncio
async def test_manager_cancel_awaits_generation_task_and_platform(tmp_path) -> None:
    manager = RunManager(tmp_path)
    run = make_run(tmp_path)
    run.status = WebRunStatus.RUNNING
    run.current_stage = "実行中"
    run.generations[0].status = "active"
    manager.store.create(run)
    manager._runs[run.run_id] = run

    task_finished = asyncio.Event()

    async def generation() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            task_finished.set()

    class PlatformStub:
        def __init__(self) -> None:
            self.shutdown_count = 0

        async def shutdown(self) -> None:
            self.shutdown_count += 1

    task = asyncio.create_task(generation(), name="web-run[test-cancel]")
    await asyncio.sleep(0)
    platform = PlatformStub()
    manager._tasks[run.run_id] = task
    manager._platforms[run.run_id] = platform  # type: ignore[assignment]

    cancelled = await manager.cancel(run.run_id)

    assert cancelled.status is WebRunStatus.CANCELLED
    assert task.done()
    assert task_finished.is_set()
    assert run.run_id not in manager._tasks
    assert run.run_id not in manager._platforms
    assert platform.shutdown_count == 1
