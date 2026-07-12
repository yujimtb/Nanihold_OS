"""対話コンソールのセッション継続・復元・多重送信契約。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from vsm.agents.backends.fake import FakeAgentRuntime
from vsm.agents.runtime import AgentResult
from vsm.web.chat import ChatManager
from vsm.web import app as web_app_module


def test_chat_session_two_turns_restore_and_reject_busy(tmp_path, monkeypatch) -> None:
    runtime: FakeAgentRuntime

    def response(_request):
        return AgentResult(
            text=f"応答{len(runtime.invocations)}",
            tokens_in=3,
            tokens_out=5,
            tokens_cache_read=1,
            latency_ms=12,
            model="fake-chat-model",
            backend="fake",
            session_ref=f"session-{len(runtime.invocations)}",
        )

    runtime = FakeAgentRuntime(response=response)
    manager = ChatManager(
        tmp_path / "chat",
        runtime_factory=lambda _backend, _model: runtime,
        default_workdir=tmp_path,
    )
    monkeypatch.setattr(web_app_module, "chat_manager", manager)
    client = TestClient(web_app_module.app)

    created = client.post(
        "/api/chat",
        json={"backend": "claude-code", "model": "claude-test", "workdir": str(tmp_path)},
    )
    assert created.status_code == 201
    chat_id = created.json()["chat_id"]

    first = client.post(f"/api/chat/{chat_id}/messages", json={"text": "最初の依頼"})
    second = client.post(f"/api/chat/{chat_id}/messages", json={"text": "続きの依頼"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["text"] == "応答1"
    assert second.json()["text"] == "応答2"
    assert runtime.invocations[0].session_ref is None
    assert runtime.invocations[1].session_ref == "session-1"
    assert second.json()["tokens"] == 8
    assert second.json()["latency_ms"] == 12

    restored_runtime = FakeAgentRuntime(response="復元後の応答")
    restored = ChatManager(
        tmp_path / "chat",
        runtime_factory=lambda _backend, _model: restored_runtime,
        default_workdir=tmp_path,
    )
    history = restored.history(chat_id)
    assert history["session_ref"] == "session-2"
    assert history["total_tokens"] == 16
    assert [message["role"] for message in history["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]

    entered = threading.Event()
    runtime.latency = 0.15
    runtime.response = lambda _request: entered.set() or "長い応答"
    with ThreadPoolExecutor(max_workers=1) as executor:
        first_in_flight = executor.submit(
            client.post,
            f"/api/chat/{chat_id}/messages",
            json={"text": "処理中の依頼"},
        )
        assert entered.wait(5)
        busy = client.post(f"/api/chat/{chat_id}/messages", json={"text": "同時送信"})
        assert first_in_flight.result().status_code == 200
    assert busy.status_code == 409
