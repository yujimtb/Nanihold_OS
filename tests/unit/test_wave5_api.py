"""Wave 5 REST/CLI/topology contracts without starting a real Run."""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from typer.testing import CliRunner

from vsm.cli import app as cli_app
from vsm.config import AgentsConfig, RunConfig
from vsm.eventlog.reader import read_all
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform
from vsm.web import app as web_app_module
from vsm.web.topology import project_budget, project_topology


class FakeManager:
    def __init__(self) -> None:
        self.created: dict | None = None

    async def create_run(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(run_id="run-1234567890abcdef1234567890abcdef")

    def detail(self, run_id):
        return {"run_id": run_id, "status": "queued"}

    async def instruct(self, run_id, instruction, target_node):
        return {"run_id": run_id, "instruction_id": "i-1", "delivered": True}

    async def raise_algedonic(self, run_id, **kwargs):
        return {"run_id": run_id, "invocation_id": "a-1", "delivered": True}

    def submit_consortium_statement(self, consortium_id, statement):
        return {"consortium_id": consortium_id, "accepted": True}

    def topology(self, run_id):
        return {"run_id": run_id, "nodes": [], "pending_human_reviews": [], "waiting_consortiums": []}

    def budget(self, run_id):
        return {"run_id": run_id, "limit": {"tokens": 100}, "consumed": {}, "nodes": {}}

    async def control_node(self, run_id, node_id, action):
        return {"run_id": run_id, "node_id": node_id, "status": "SUSPENDED"}

    async def respond_human_review(self, run_id, review_key, response):
        return {"run_id": run_id, "review_key": review_key, "accepted": True}


def test_wave5_rest_endpoints(monkeypatch):
    manager = FakeManager()
    monkeypatch.setattr(web_app_module, "manager", manager)
    client = TestClient(web_app_module.app)
    run_id = "run-1234567890abcdef1234567890abcdef"

    response = client.post(
        "/api/runs",
        json={
            "goal": "市場を調査する",
            "constraints": {"language": "ja"},
            "budget": {"tokens": 1200, "wall_clock_seconds": 60},
        },
    )
    assert response.status_code == 201
    assert manager.created == {
        "description": "市場を調査する",
        "title": None,
        "attachments": [],
        "constraints": {"language": "ja"},
        "budget_override": {"tokens": 1200, "wall_clock_seconds": 60.0},
    }
    assert client.post(
        f"/api/runs/{run_id}/instructions",
        json={"instruction": "保守性を優先", "target_node": "node-s5"},
    ).json()["delivered"] is True
    assert client.post(
        f"/api/runs/{run_id}/algedonic",
        json={"severity": "pain", "reason": "期限超過", "source_node_id": "node-s5"},
    ).json()["delivered"] is True
    assert client.post(
        "/api/consortium/c-1/statement", json={"statement": "段階導入に賛成"}
    ).json()["accepted"] is True
    assert client.get(f"/api/runs/{run_id}/topology").status_code == 200
    assert client.get(f"/api/runs/{run_id}/budget").json()["limit"]["tokens"] == 100


def test_topology_and_budget_are_rebuilt_from_events():
    events = [
        {"event_type": "budget_configured", "payload": {"run_tokens": 1000, "run_wall_clock_seconds": 20}},
        {"event_type": "node_created", "payload": {"node_id": "s5", "parent_id": None, "vsm_position": "S5_POLICY", "terminable": False}},
        {"event_type": "agent_attached", "node_id": "s5", "payload": {"node_id": "s5", "backend": "codex", "model": "gpt", "budget": {"tokens": 1000, "wall_clock_seconds": 20}}},
        {"event_type": "node_started", "payload": {"node_id": "s5", "status": "RUNNING"}},
        {"event_type": "instruction_received", "payload": {"instruction_id": "i-1", "instruction": "品質優先", "target_node": "s5"}},
        {"event_type": "llm_invocation", "node_id": "s5", "payload": {"backend": "codex", "model": "gpt", "response": "設計を確認中"}},
        {"event_type": "budget_consumed", "node_id": "s5", "payload": {"node_id": "s5", "cumulative": {"tokens_total": 30, "wall_clock_ms": 400}, "run_cumulative": {"tokens_total": 30, "wall_clock_ms": 400}}},
    ]
    topology = project_topology(events, "run-x")
    assert topology["nodes"][0]["status"] == "RUNNING"
    assert topology["nodes"][0]["authority"]["kind"] == "instruction"
    assert topology["nodes"][0]["budget"]["tokens_consumed"] == 30
    budget = project_budget(events, "run-x")
    assert budget["limit"]["tokens"] == 1000
    assert budget["consumed"]["wall_clock_seconds"] == 0.4


def test_instruct_cli_posts_json(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"delivered": true}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = CliRunner().invoke(
        cli_app,
        ["instruct", "run-1234567890abcdef1234567890abcdef", "品質優先", "--node", "node-s5"],
    )
    assert result.exit_code == 0
    assert captured["url"].endswith("/instructions")
    assert captured["payload"] == {"instruction": "品質優先", "target_node": "node-s5"}


@pytest.mark.asyncio
async def test_instruction_is_logged_and_delivered_as_message(tmp_path):
    roles = {role: "fake" for role in SystemRole}
    roles[SystemRole.S3_ALLOCATOR] = ""
    platform = await Platform.create(
        run_id="run-wave5-instruction",
        runs_dir=tmp_path,
        run_config=RunConfig(agents=AgentsConfig(default_backend="fake", roles=roles)),
    )
    await platform.start()
    try:
        target = platform.systems[SystemRole.S5_POLICY][0].system_id
        instruction_id = await platform.deliver_instruction("品質を優先する")
    finally:
        await platform.shutdown()

    events = read_all(platform.run_dir / "events.jsonl")
    received = next(event for event in events if event["event_type"] == "instruction_received")
    delivered = next(
        event
        for event in events
        if event["event_type"] == "channel_message"
        and event["payload"]["channel"] == "INSTRUCTION"
    )
    assert received["payload"] == {
        "instruction_id": instruction_id,
        "instruction": "品質を優先する",
        "target_node": target,
        "source": "human",
    }
    assert delivered["payload"]["receiver"] == target
