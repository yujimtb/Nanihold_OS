"""Asynchronous single-run manager for the local web application."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vsm.config import load_config
from vsm.eventlog.reader import read_all
from vsm.ids import generate_run_id, generate_uuid
from vsm.llm.fake import FakeLLMProvider
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform, start_run
from vsm.web.models import RunGeneration, WebRun, WebRunStatus
from vsm.web.projection import project_event
from vsm.web.store import RunStore

MAX_AUTOMATIC_ATTEMPTS = 2
COMPLETION_TIMEOUT_SECONDS = 1800
SETTLE_SECONDS = 1.5


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class RunManager:
    def __init__(self, runs_root: Path) -> None:
        self.store = RunStore(runs_root)
        self._runs: dict[str, WebRun] = {run.run_id: run for run in self.store.list()}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._platforms: dict[str, Platform] = {}
        self._lock = asyncio.Lock()
        self._active_run_id: str | None = None
        for run in self._runs.values():
            if run.status in {
                WebRunStatus.QUEUED,
                WebRunStatus.RUNNING,
                WebRunStatus.INTERRUPTING,
            }:
                run.status = WebRunStatus.FAILED
                run.error = "アプリケーションの再起動により実行が中断されました。"
                run.updated_at = utc_now()
                self.store.record_state(run, "application_restarted")

    def list_runs(self) -> list[dict[str, Any]]:
        return [self._summary(run) for run in sorted(self._runs.values(), key=lambda item: item.updated_at, reverse=True)]

    def get_run(self, run_id: str) -> WebRun:
        run = self._runs.get(run_id)
        if run is None:
            run = self.store.load(run_id)
            self._runs[run_id] = run
        return run

    async def create_run(
        self,
        *,
        description: str,
        title: str | None,
        attachments: list,
    ) -> WebRun:
        cleaned = description.strip()
        if not 1 <= len(cleaned) <= 8192:
            raise ValueError("タスクは1文字以上8192文字以下で入力してください")
        async with self._lock:
            if self._active_run_id is not None:
                active = self._runs.get(self._active_run_id)
                if active and active.status in {
                    WebRunStatus.QUEUED,
                    WebRunStatus.RUNNING,
                    WebRunStatus.INTERRUPTING,
                    WebRunStatus.WAITING_FOR_USER,
                }:
                    raise RuntimeError("現在実行中のタスクがあります。完了または停止してから開始してください。")
            run_id = generate_run_id()
            now = utc_now()
            run_dir = self.store.root / run_id
            run = WebRun(
                run_id=run_id,
                title=(title or self._make_title(cleaned)).strip()[:80],
                description=cleaned,
                created_at=now,
                updated_at=now,
                status=WebRunStatus.QUEUED,
                run_dir=run_dir,
                attachments=attachments,
            )
            self._runs[run_id] = run
            self._active_run_id = run_id
            self.store.create(run)
            self._start_generation(run, instruction="")
            return run

    async def cancel(self, run_id: str) -> WebRun:
        run = self.get_run(run_id)
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        platform = self._platforms.get(run_id)
        if platform is not None:
            await platform.shutdown()
            self._platforms.pop(run_id, None)
        run.status = WebRunStatus.CANCELLED
        run.current_stage = "キャンセル済み"
        run.updated_at = utc_now()
        self._mark_generation(run, "cancelled")
        self._append_control(run, "run_cancelled", {"reason": "user_requested"})
        self._release_active(run_id)
        self.store.record_state(run, "cancelled_by_user")
        return run

    async def interrupt(self, run_id: str, instruction: str) -> WebRun:
        run = self.get_run(run_id)
        cleaned = instruction.strip()
        if not cleaned:
            raise ValueError("追加指示を入力してください")
        if run.status not in {
            WebRunStatus.QUEUED,
            WebRunStatus.RUNNING,
            WebRunStatus.INTERRUPTING,
        }:
            raise RuntimeError("追加指示は準備中または実行中のタスクにのみ送信できます")

        run.status = WebRunStatus.INTERRUPTING
        run.pending_instruction = cleaned
        run.updated_at = utc_now()
        self._append_control(
            run,
            "instruction_received",
            {"instruction": cleaned, "generation": run.generation},
        )
        self.store.record_state(run, "instruction_received")

        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._mark_generation(run, "superseded")
        run.pending_instruction = None
        run.status = WebRunStatus.QUEUED
        self._start_generation(run, instruction=cleaned)
        return run

    async def retry(self, run_id: str) -> WebRun:
        run = self.get_run(run_id)
        if run.status not in {WebRunStatus.FAILED, WebRunStatus.WAITING_FOR_USER}:
            raise RuntimeError("再試行できる状態ではありません")
        async with self._lock:
            if self._active_run_id not in {None, run_id}:
                raise RuntimeError("別のタスクが実行中です")
            self._active_run_id = run_id
        run.error = None
        run.status = WebRunStatus.QUEUED
        self._append_control(run, "retry_started", {})
        self._start_generation(run, instruction=run.pending_instruction or "")
        return run

    async def use_partial_result(self, run_id: str) -> WebRun:
        run = self.get_run(run_id)
        events = self._all_runtime_events(run)
        run.final_answer = self._fallback_answer(events)
        run.status = WebRunStatus.COMPLETED
        run.current_stage = "部分結果を保存"
        run.progress = 100
        run.updated_at = utc_now()
        self._append_control(run, "partial_result_accepted", {})
        self._write_artifacts(run)
        self._release_active(run_id)
        self.store.record_state(run, "partial_result_accepted")
        return run

    def delete(self, run_id: str) -> None:
        run = self.get_run(run_id)
        if run.status in {WebRunStatus.RUNNING, WebRunStatus.INTERRUPTING}:
            raise RuntimeError("実行中のタスクは削除できません")
        self._runs.pop(run_id, None)
        self.store.delete(run_id)

    def rename(self, run_id: str, title: str) -> WebRun:
        run = self.get_run(run_id)
        cleaned = title.strip()
        if not cleaned:
            raise ValueError("Run名を入力してください")
        run.title = cleaned[:80]
        run.updated_at = utc_now()
        self.store.append_event(
            run,
            "web_run_renamed",
            {"title": run.title, "updated_at": run.updated_at},
            actor_type="human",
            actor_id="local-user",
        )
        self.store.save(run)
        return run

    def detail(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        return {
            **self._summary(run),
            "description": run.description,
            "final_answer": run.final_answer,
            "error": run.error,
            "attachments": [attachment.public_dict() for attachment in run.attachments],
            "artifacts": self.artifacts(run),
            "timeline": self._timeline(run),
            "generations": [generation.__dict__ for generation in run.generations],
        }

    def artifacts(self, run: WebRun) -> list[dict[str, Any]]:
        directory = run.run_dir / "artifacts"
        if not directory.exists():
            return []
        return [
            {
                "name": path.name,
                "size": path.stat().st_size,
                "media_type": (
                    "text/markdown"
                    if path.suffix == ".md"
                    else "application/json"
                ),
            }
            for path in sorted(directory.iterdir())
            if path.is_file()
        ]

    def artifact_path(self, run_id: str, name: str) -> Path:
        run = self.get_run(run_id)
        if Path(name).name != name:
            raise FileNotFoundError(name)
        path = run.run_dir / "artifacts" / name
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    async def stream(self, run_id: str) -> AsyncIterator[str]:
        previous = ""
        while True:
            payload = self.detail(run_id)
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
            if encoded != previous:
                yield f"event: run\ndata: {encoded}\n\n"
                previous = encoded
            if payload["status"] in {"completed", "cancelled", "failed"}:
                return
            await asyncio.sleep(0.35)

    def _start_generation(self, run: WebRun, instruction: str) -> None:
        generation = run.generation + 1
        runtime_run_id = generate_run_id()
        run.generations.append(
            RunGeneration(
                generation=generation,
                runtime_run_id=runtime_run_id,
                instruction=instruction,
                started_at=utc_now(),
            )
        )
        run.status = WebRunStatus.QUEUED
        run.current_stage = "実行準備"
        run.progress = 2
        run.updated_at = utc_now()
        self.store.append_event(
            run,
            "web_generation_started",
            {
                "generation": generation,
                "runtime_run_id": runtime_run_id,
                "instruction": instruction,
                "started_at": run.generations[-1].started_at,
            },
        )
        self.store.record_state(run, "generation_started")
        self._tasks[run.run_id] = asyncio.create_task(
            self._execute_generation(run, generation, runtime_run_id, instruction),
            name=f"web-run[{run.run_id}:g{generation}]",
        )

    async def _execute_generation(
        self,
        run: WebRun,
        generation: int,
        runtime_run_id: str,
        instruction: str,
    ) -> None:
        platform: Platform | None = None
        try:
            run.status = WebRunStatus.RUNNING
            run.current_stage = "VSMを起動"
            run.updated_at = utc_now()
            self.store.record_state(run, "platform_starting")

            llm_config, run_config = load_config(None)
            use_fake = os.environ.get("NANIHOLD_USE_FAKE_LLM", "").lower() in {"1", "true", "yes"}
            if use_fake or not (llm_config.provider_from_env or llm_config.provider_from_file):
                llm_override = FakeLLMProvider(
                    response=lambda prompt, _model: self._fake_response(prompt),
                    latency=0.08,
                )
            else:
                llm_override = None

            runtime_root = run.run_dir / "runtime"
            platform = await start_run(
                run_id=runtime_run_id,
                runs_dir=runtime_root,
                run_config=run_config,
                llm_config=llm_config,
                llm_override=llm_override,
            )
            self._platforms[run.run_id] = platform

            task_payload = {
                "task_id": generate_uuid(),
                "run_id": runtime_run_id,
                "description": run.description,
                "file_paths": [str(attachment.path) for attachment in run.attachments],
                "submitted_at": utc_now(),
            }
            await platform.eventlog.append("task_submitted", task_payload)

            prompt_context = self._build_task_context(run, instruction)
            s4 = platform.systems[SystemRole.S4_SCANNER][0]
            await s4.trigger(
                {
                    **task_payload,
                    "description": prompt_context,
                    "generation": generation,
                }
            )

            events_path = platform.run_dir / "events.jsonl"
            events = await self._wait_for_completion(run, events_path)
            run.current_stage = "最終回答を統合"
            run.progress = 96
            run.updated_at = utc_now()
            self.store.record_state(run, "final_answer_synthesis")
            run.final_answer = await self._synthesise_answer(platform, run, events, instruction)
            run.status = WebRunStatus.COMPLETED
            run.current_stage = "完了"
            run.progress = 100
            run.error = None
            run.updated_at = utc_now()
            self._mark_generation(run, "completed")
            self._append_control(
                run,
                "run_completed",
                {
                    "generation": generation,
                    "answer_ref": "artifacts/final-answer.md",
                },
            )
            self._write_artifacts(run)
            self._release_active(run.run_id)
            self.store.record_state(run, "completed")
        except asyncio.CancelledError:
            if platform is not None:
                await platform.shutdown()
                platform = None
            raise
        except Exception as exc:
            if generation < MAX_AUTOMATIC_ATTEMPTS:
                self._mark_generation(run, "failed")
                self._append_control(
                    run,
                    "automatic_retry",
                    {"generation": generation, "reason": str(exc)},
                )
                self._start_generation(run, instruction)
            else:
                run.status = WebRunStatus.WAITING_FOR_USER
                run.current_stage = "ユーザー判断待ち"
                run.error = str(exc)
                run.updated_at = utc_now()
                self._mark_generation(run, "failed")
                self.store.record_state(run, "automatic_retries_exhausted")
        finally:
            if platform is not None:
                await platform.shutdown()
            self._platforms.pop(run.run_id, None)

    async def _wait_for_completion(self, run: WebRun, events_path: Path) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + COMPLETION_TIMEOUT_SECONDS
        completion_seen_at: float | None = None
        latest_events: list[dict[str, Any]] = []
        while loop.time() < deadline:
            await asyncio.sleep(0.25)
            if not events_path.exists():
                continue
            latest_events = read_all(events_path)
            count = sum(event.get("event_type") == "s1_completion" for event in latest_events)
            stage, progress = self._stage_from_events(latest_events)
            if stage != run.current_stage or progress != run.progress:
                run.current_stage = stage
                run.progress = progress
                run.updated_at = utc_now()
                self.store.record_state(run, f"runtime_stage:{stage}")
            if count and completion_seen_at is None:
                completion_seen_at = loop.time()
            if (
                completion_seen_at is not None
                and loop.time() - completion_seen_at >= SETTLE_SECONDS
            ):
                return latest_events
        raise TimeoutError("実行が30分以内に完了しませんでした")

    async def _synthesise_answer(
        self,
        platform: Platform,
        run: WebRun,
        events: list[dict[str, Any]],
        instruction: str,
    ) -> str:
        results = [
            (event.get("payload") or {}).get("result", {}).get("text", "")
            for event in events
            if event.get("event_type") == "s1_completion"
        ]
        results = [result for result in results if result]
        if not results:
            return self._fallback_answer(events)
        prompt = (
            "あなたはNanihold OSの最終編集者です。ユーザーの依頼と各担当の"
            "実行結果を統合し、日本語のMarkdownで直接役立つ最終回答を作ってください。"
            "内部の思考過程は開示せず、結論、根拠となる要点、必要な手順や成果物を"
            "人間が理解できる具体性で示してください。\n\n"
            f"依頼:\n{run.description}\n\n"
            f"追加指示:\n{instruction or 'なし'}\n\n"
            f"担当結果:\n" + "\n\n---\n\n".join(results)
        )
        s5 = platform.systems[SystemRole.S5_POLICY][0]
        try:
            response = await s5.sub_agents[0].respond(prompt)
            return response.text.strip() or self._fallback_answer(events)
        except Exception:
            return self._fallback_answer(events)

    def _build_task_context(self, run: WebRun, instruction: str) -> str:
        sections = [f"ユーザー依頼:\n{run.description}"]
        if instruction:
            sections.append(f"最新の追加指示（以前の実行より優先）:\n{instruction}")
        for attachment in run.attachments:
            if attachment.extracted_text:
                sections.append(
                    f"添付ファイル: {attachment.name}\n"
                    f"{attachment.extracted_text[:100_000]}"
                )
            elif attachment.model_content:
                sections.append(
                    f"添付ファイル: {attachment.name}\n"
                    "画像形式のため、固定モデルが画像入力に対応している場合に解析対象です。"
                )
        return "\n\n".join(sections)

    def _timeline(self, run: WebRun) -> list[dict[str, Any]]:
        timeline: list[dict[str, Any]] = []
        for generation in run.generations:
            path = run.run_dir / "runtime" / generation.runtime_run_id / "events.jsonl"
            if not path.exists():
                continue
            superseded = generation.status == "superseded"
            for event in read_all(path):
                projected = project_event(event, generation.generation, superseded)
                if projected:
                    timeline.append(projected)
        control_types = {
            "web_instruction_received",
            "web_retry_started",
            "web_run_cancelled",
            "web_run_completed",
            "web_partial_result_accepted",
        }
        for event in self.store.read_events(run):
            if event["event_type"] in control_types:
                payload = event.get("payload", {})
                timeline.append(
                    {
                        "id": event["event_id"],
                        "generation": payload.get("generation", run.generation),
                        "seq": 1_000_000 + event["seq"],
                        "ts": event.get("ts"),
                        "type": event["event_type"],
                        "stage": "ユーザー操作",
                        "progress": run.progress,
                        "system": "You",
                        "title": {
                            "web_instruction_received": "追加指示を受け付けました",
                            "web_retry_started": "再試行を開始しました",
                            "web_run_cancelled": "実行を停止しました",
                            "web_run_completed": "最終回答を確定しました",
                            "web_partial_result_accepted": "部分結果を採用しました",
                        }.get(event["event_type"], event["event_type"]),
                        "summary": payload.get("instruction")
                        or payload.get("reason")
                        or "",
                        "details": payload,
                        "superseded": False,
                    }
                )
        return sorted(timeline, key=lambda item: (item["ts"] or "", item["seq"]))

    def _all_runtime_events(self, run: WebRun) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for generation in run.generations:
            path = run.run_dir / "runtime" / generation.runtime_run_id / "events.jsonl"
            if path.exists():
                events.extend(read_all(path))
        return events

    @staticmethod
    def _fallback_answer(events: list[dict[str, Any]]) -> str:
        results = [
            (event.get("payload") or {}).get("result", {}).get("text", "")
            for event in events
            if event.get("event_type") == "s1_completion"
        ]
        results = [result for result in results if result]
        return "\n\n".join(f"### 担当結果 {index}\n\n{result}" for index, result in enumerate(results, 1)) or "有効な部分結果はありません。"

    @staticmethod
    def _stage_from_events(events: list[dict[str, Any]]) -> tuple[str, int]:
        for event_type, stage, progress in (
            ("audit_finding", "監査", 92),
            ("s1_completion", "作業実行", 82),
            ("s1_assignment_sent", "作業割当", 65),
            ("s1_instantiated", "実行チーム編成", 55),
            ("policy_decision", "方針決定", 45),
            ("s4_assessment_produced", "環境分析", 25),
            ("task_submitted", "受付", 5),
        ):
            if any(event.get("event_type") == event_type for event in events):
                return stage, progress
        return "VSMを起動", 3

    def _append_control(self, run: WebRun, event_type: str, payload: dict[str, Any]) -> None:
        mapped_type = {
            "instruction_received": "web_instruction_received",
            "automatic_retry": "web_retry_started",
            "retry_started": "web_retry_started",
            "run_cancelled": "web_run_cancelled",
            "run_completed": "web_run_completed",
            "partial_result_accepted": "web_partial_result_accepted",
        }[event_type]
        self.store.append_event(
            run,
            mapped_type,
            {"generation": run.generation, **payload},
            actor_type="human" if event_type in {
                "instruction_received",
                "retry_started",
                "run_cancelled",
                "partial_result_accepted",
            } else "system",
            actor_id="local-user" if event_type in {
                "instruction_received",
                "retry_started",
                "run_cancelled",
                "partial_result_accepted",
            } else "web-runtime",
        )

    def _write_artifacts(self, run: WebRun) -> None:
        directory = run.run_dir / "artifacts"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "final-answer.md").write_text(
            run.final_answer or "有効な結果はありません。",
            encoding="utf-8",
        )
        (directory / "process-log.json").write_text(
            json.dumps(self._timeline(run), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        for name, media_type in (
            ("final-answer.md", "text/markdown"),
            ("process-log.json", "application/json"),
        ):
            path = directory / name
            self.store.append_event(
                run,
                "artifact_created",
                {
                    "artifact_ref": str(path.relative_to(run.run_dir)),
                    "name": name,
                    "media_type": media_type,
                    "size": path.stat().st_size,
                },
            )

    def _mark_generation(self, run: WebRun, status: str) -> None:
        if run.generations:
            run.generations[-1].status = status
            run.generations[-1].finished_at = utc_now()
            self.store.append_event(
                run,
                "web_generation_finished",
                {
                    "generation": run.generations[-1].generation,
                    "status": status,
                    "finished_at": run.generations[-1].finished_at,
                },
            )

    def _release_active(self, run_id: str) -> None:
        if self._active_run_id == run_id:
            self._active_run_id = None

    @staticmethod
    def _make_title(description: str) -> str:
        first_line = description.splitlines()[0].strip()
        return first_line[:42] + ("…" if len(first_line) > 42 else "")

    @staticmethod
    def _fake_response(prompt: str) -> str:
        if "最終編集者" in prompt:
            return (
                "## 実行結果\n\n"
                "依頼内容をVSMの各担当で分析・実行し、監査まで完了しました。\n\n"
                "### 要点\n\n"
                "- 環境分析、方針決定、作業割当、実行、監査の順で処理しました。\n"
                "- 現在はデモ用モデルで動作しています。`.env`に実モデルを設定すると実回答へ切り替わります。"
            )
        return "依頼内容を確認し、担当範囲の分析と実行を完了しました。"

    @staticmethod
    def _summary(run: WebRun) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "title": run.title,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "status": run.status.value,
            "current_stage": run.current_stage,
            "progress": run.progress,
            "generation": run.generation,
        }
