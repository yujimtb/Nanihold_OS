from __future__ import annotations

import json
import re

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


def test_run_store_round_trip_preserves_japanese(tmp_path) -> None:
    store = RunStore(tmp_path)
    run = make_run(tmp_path)

    store.save(run)
    loaded = store.load(run.run_id)

    assert loaded.title == "日本語のテスト"
    assert loaded.description == "市場調査を行ってください"
    assert loaded.final_answer == "# 調査結果\n\n完了しました。"
    assert loaded.generations[0].status == "completed"


def test_manager_writes_downloadable_artifacts(tmp_path) -> None:
    manager = RunManager(tmp_path)
    run = make_run(tmp_path)
    manager.store.save(run)
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
