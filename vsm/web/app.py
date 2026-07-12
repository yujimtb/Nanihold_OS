"""FastAPI application for the local Nanihold OS dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from vsm.agents.runtime import AgentRuntimeError
from vsm.config import load_config
from vsm.runtime.lifecycle import describe_role_runtimes
from vsm.web.chat import ChatBusyError, ChatManager, ChatTimeoutError
from vsm.web.manager import RunManager

RUNS_ROOT = Path("runs") / "web"
manager = RunManager(RUNS_ROOT)
chat_manager = ChatManager(RUNS_ROOT / "chat")

app = FastAPI(title="Nanihold OS", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    # 開発中はループバック上の任意ポート(予備の Vite ポート等)を許可する。
    # ループバック以外のオリジンは引き続き拒否される。
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class InstructionBody(BaseModel):
    instruction: str
    target_node: str | None = None


class BudgetOverrideBody(BaseModel):
    tokens: int | None = Field(default=None, gt=0)
    wall_clock_seconds: float | None = Field(default=None, gt=0)


class CreateRunBody(BaseModel):
    goal: str
    constraints: dict = Field(default_factory=dict)
    budget: BudgetOverrideBody | None = None


class AlgedonicBody(BaseModel):
    severity: str
    reason: str
    source_node_id: str


class StatementBody(BaseModel):
    statement: str


class NodeControlBody(BaseModel):
    action: str


class HumanReviewBody(BaseModel):
    review_key: str
    response: str


class RenameBody(BaseModel):
    title: str


class CreateChatBody(BaseModel):
    backend: Literal["claude-code", "codex"]
    model: str | None = None
    workdir: str | None = None


class ChatMessageBody(BaseModel):
    text: str


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/chat", status_code=201)
def create_chat(body: CreateChatBody) -> dict:
    try:
        return chat_manager.create_session(
            backend=body.backend,
            model=body.model,
            workdir=body.workdir,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/chat/{chat_id}")
def get_chat(chat_id: str) -> dict:
    try:
        return chat_manager.history(chat_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="対話セッションが見つかりません") from exc


@app.post("/api/chat/{chat_id}/messages")
async def send_chat_message(chat_id: str, body: ChatMessageBody) -> dict:
    try:
        return await chat_manager.send_message(chat_id, body.text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="対話セッションが見つかりません") from exc
    except ChatBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ChatTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (AgentRuntimeError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/config")
def config() -> dict:
    llm_config, run_config = load_config(None)
    runtimes = describe_role_runtimes(run_config=run_config, llm_config=llm_config)
    return {
        "runtimes": runtimes,
        "demo_mode": any(runtime["backend"] == "fake" for runtime in runtimes),
        "single_run": True,
    }


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return manager.list_runs()


@app.post("/api/runs", status_code=201)
async def create_run(
    body: CreateRunBody,
) -> dict:
    try:
        run = await manager.create_run(
            description=body.goal,
            title=None,
            attachments=[],
            constraints=body.constraints,
            budget_override=(body.budget.model_dump(exclude_none=True) if body.budget else None),
        )
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


@app.post("/api/runs/{run_id}/instructions")
async def instruct(run_id: str, body: InstructionBody) -> dict:
    try:
        return await manager.instruct(run_id, body.instruction, body.target_node)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Runが見つかりません") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/algedonic")
async def algedonic(run_id: str, body: AlgedonicBody) -> dict:
    try:
        return await manager.raise_algedonic(
            run_id,
            severity=body.severity,
            reason=body.reason,
            source_node_id=body.source_node_id,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/consortium/{consortium_id}/statement")
def consortium_statement(consortium_id: str, body: StatementBody) -> dict:
    try:
        return manager.submit_consortium_statement(consortium_id, body.statement)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}/topology")
def topology(run_id: str) -> dict:
    try:
        return manager.topology(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Runが見つかりません") from exc


@app.get("/api/runs/{run_id}/budget")
def budget(run_id: str) -> dict:
    try:
        return manager.budget(run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Runが見つかりません") from exc


@app.post("/api/runs/{run_id}/nodes/{node_id}/control")
async def control_node(run_id: str, node_id: str, body: NodeControlBody) -> dict:
    try:
        return await manager.control_node(run_id, node_id, body.action)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/human-review")
async def human_review(run_id: str, body: HumanReviewBody) -> dict:
    try:
        return await manager.respond_human_review(run_id, body.review_key, body.response)
    except KeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
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
