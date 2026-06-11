"""FastAPI application for the local Nanihold OS dashboard."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from vsm.config import load_config
from vsm.web.attachments import save_attachments
from vsm.web.manager import RunManager

RUNS_ROOT = Path(os.environ.get("NANIHOLD_RUNS_DIR", "runs")) / "web"
manager = RunManager(RUNS_ROOT)

app = FastAPI(title="Nanihold OS", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class InstructionBody(BaseModel):
    instruction: str


class RenameBody(BaseModel):
    title: str


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config")
def config() -> dict:
    llm_config, _ = load_config(None)
    configured_model = llm_config.provider_from_env or llm_config.provider_from_file
    return {
        "model": configured_model or "fake/test-model",
        "demo_mode": not bool(configured_model),
        "single_run": True,
    }


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return manager.list_runs()


@app.post("/api/runs", status_code=201)
async def create_run(
    description: str = Form(...),
    title: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    run_id = None
    try:
        from vsm.ids import generate_run_id

        run_id = generate_run_id()
        temp_dir = RUNS_ROOT / ".uploads" / run_id
        attachments = await save_attachments(files, temp_dir)
        run = await manager.create_run(
            description=description,
            title=title,
            attachments=attachments,
        )
        destination = run.run_dir / "attachments"
        destination.mkdir(parents=True, exist_ok=True)
        for attachment in attachments:
            new_path = destination / attachment.path.name
            attachment.path.replace(new_path)
            attachment.path = new_path
        if temp_dir.exists():
            temp_dir.rmdir()
        manager.store.save(run)
        return manager.detail(run.run_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409 if isinstance(exc, RuntimeError) else 422, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    try:
        return manager.detail(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Runが見つかりません") from exc


@app.get("/api/runs/{run_id}/events")
def stream_run(run_id: str) -> StreamingResponse:
    try:
        manager.get_run(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Runが見つかりません") from exc
    return StreamingResponse(
        manager.stream(run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/runs/{run_id}/interrupt")
async def interrupt(run_id: str, body: InstructionBody) -> dict:
    try:
        run = await manager.interrupt(run_id, body.instruction)
        return manager.detail(run.run_id)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/cancel")
async def cancel(run_id: str) -> dict:
    run = await manager.cancel(run_id)
    return manager.detail(run.run_id)


@app.post("/api/runs/{run_id}/retry")
async def retry(run_id: str) -> dict:
    try:
        run = await manager.retry(run_id)
        return manager.detail(run.run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/use-partial")
async def use_partial(run_id: str) -> dict:
    run = await manager.use_partial_result(run_id)
    return manager.detail(run.run_id)


@app.patch("/api/runs/{run_id}")
def rename(run_id: str, body: RenameBody) -> dict:
    try:
        return manager.detail(manager.rename(run_id, body.title).run_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/api/runs/{run_id}", status_code=204)
def delete(run_id: str) -> None:
    try:
        manager.delete(run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}/attachments/{attachment_id}")
def download_attachment(run_id: str, attachment_id: str) -> FileResponse:
    run = manager.get_run(run_id)
    attachment = next(
        (item for item in run.attachments if item.attachment_id == attachment_id),
        None,
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail="添付ファイルが見つかりません")
    return FileResponse(attachment.path, filename=attachment.name, media_type=attachment.media_type)


@app.get("/api/runs/{run_id}/artifacts/{name}")
def download_artifact(run_id: str, name: str) -> FileResponse:
    try:
        path = manager.artifact_path(run_id, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="成果物が見つかりません") from exc
    media_type = "text/markdown" if path.suffix == ".md" else "application/json"
    return FileResponse(path, filename=path.name, media_type=media_type)
