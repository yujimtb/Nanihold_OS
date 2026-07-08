"""Submit CLI configuration loading tests."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from vsm.cli import app
from vsm.config import LITELLM_PROVIDER_ENV, LLMConfig, RunConfig
from vsm.roles import SystemRole


runner = CliRunner()


def test_submit_loads_dotenv_config_before_start_run(tmp_path, monkeypatch) -> None:
    """``vsm submit`` loads ``.env`` and passes config into ``start_run``."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(LITELLM_PROVIDER_ENV, raising=False)
    (tmp_path / ".env").write_text(
        "LITELLM_PROVIDER=openrouter/test-model\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    class FakeEventLog:
        async def append(self, _event_type: str, _payload: dict) -> None:
            return None

    class FakeS4:
        async def trigger(self, _payload: dict) -> None:
            return None

    class FakePlatform:
        def __init__(self, run_dir):
            self.run_dir = run_dir
            self.eventlog = FakeEventLog()
            self.systems = {SystemRole.S4_SCANNER: [FakeS4()]}

        async def shutdown(self) -> None:
            return None

    def event(run_id: str, seq: int, event_type: str, payload: dict) -> str:
        return json.dumps(
            {
                "ts": "2026-01-01T00:00:00.000Z",
                "run_id": run_id,
                "event_type": event_type,
                "seq": seq,
                "payload": payload,
            }
        )

    async def fake_start_run(*, run_id, run_config, llm_config, **_kwargs):
        captured["run_config"] = run_config
        captured["llm_config"] = llm_config

        run_dir = tmp_path / "runs" / run_id
        run_dir.mkdir(parents=True)
        events = []
        for seq, role in enumerate(
            [
                SystemRole.S1_WORKER,
                SystemRole.S2_COORDINATOR,
                SystemRole.S3_ALLOCATOR,
                SystemRole.S3STAR_AUDITOR,
                SystemRole.S4_SCANNER,
                SystemRole.S5_POLICY,
            ]
        ):
            events.append(
                event(run_id, seq, "system_instantiated", {"role": role.value})
            )
        events.append(event(run_id, len(events), "s1_completion", {}))
        (run_dir / "events.jsonl").write_text("\n".join(events), encoding="utf-8")
        return FakePlatform(run_dir)

    import vsm.runtime.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "start_run", fake_start_run)
    monkeypatch.setattr("vsm.cli._COMPLETION_POLL_INTERVAL_SECONDS", 0.0)

    result = runner.invoke(app, ["submit", "test"])

    assert result.exit_code == 0
    assert "run_id=" in result.stdout
    assert "task_id=" in result.stdout
    assert "S4_SCANNER" in result.stderr
    assert "S1_WORKER" in result.stderr
    assert isinstance(captured["llm_config"], LLMConfig)
    assert isinstance(captured["run_config"], RunConfig)
    llm_config = captured["llm_config"]
    assert llm_config.provider_from_env == "openrouter/test-model"
    assert llm_config.resolve_model() == "openrouter/test-model"
