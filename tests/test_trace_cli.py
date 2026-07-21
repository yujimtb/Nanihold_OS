from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from typer.testing import CliRunner

from vsm.cli import app
from vsm.kernel.ledger import InMemoryOperationalLedger
from vsm.kernel.models import EventEnvelope, Execution, ExecutionState


def _execution() -> Execution:
    return Execution(
        execution_id="execution:trace",
        data_space_id="space:test",
        node_id="node:worker",
        work_item_id="work:trace",
        pilot_id="pilot:worker",
        model_candidate_key="candidate:test",
        state=ExecutionState.SUCCEEDED,
        provider_session_id="provider-session:trace",
        pilot_host_id="pilot-host:test",
        pause_reason=None,
    )


def _append(
    ledger: InMemoryOperationalLedger,
    *,
    event_id: str,
    stream_id: str,
    stream_version: int,
    event_type: str,
    payload: dict[str, object],
) -> None:
    ledger.append(
        EventEnvelope(
            event_id=event_id,
            data_space_id="space:test",
            stream_id=stream_id,
            stream_version=stream_version,
            event_type=event_type,
            occurred_at=datetime(2026, 7, 21, 12, stream_version, tzinfo=UTC),
            actor_type="system",
            actor_id="system:test",
            correlation_id="work:trace",
            causation_id=None,
            idempotency_key=f"idempotency:{event_id}",
            payload=payload,
        ),
        expected_stream_version=stream_version - 1,
    )


def _runtime() -> SimpleNamespace:
    execution = _execution()
    ledger = InMemoryOperationalLedger("space:test")
    _append(
        ledger,
        event_id="event:created",
        stream_id=execution.execution_id,
        stream_version=1,
        event_type="execution_created",
        payload={
            "execution": execution.model_copy(
                update={
                    "state": ExecutionState.REQUESTED,
                    "provider_session_id": None,
                }
            ).model_dump(mode="json")
        },
    )
    _append(
        ledger,
        event_id="event:receipt",
        stream_id=execution.execution_id,
        stream_version=2,
        event_type="pilot_execution_receipt_recorded",
        payload={
            "receipt_id": "receipt:trace",
            "receipt_status": "succeeded",
            "requested_model": "gpt-5.6-sol",
            "actual_model": "gpt-5.6-sol-20260701",
            "provider_session_id": execution.provider_session_id,
            "usage": {"input_tokens": 12, "output_tokens": 7},
            "result": {"summary": "done"},
            "error": None,
            "state": "succeeded",
            "pause_reason": None,
        },
    )
    return SimpleNamespace(
        kernel=SimpleNamespace(
            executions={execution.execution_id: execution},
            ledger=ledger,
        ),
        close=lambda: None,
    )


def test_trace_prints_ledger_receipt_and_provider_session_timeline(
    monkeypatch, tmp_path
) -> None:
    config = tmp_path / "vsm.toml"
    config.write_text("unused", encoding="utf-8")
    monkeypatch.setattr("vsm.cli.bootstrap", lambda _config: _runtime())

    result = CliRunner().invoke(
        app,
        ["trace", "execution:trace", "--config", str(config)],
    )

    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert [item["event_type"] for item in output["timeline"]] == [
        "execution_created",
        "pilot_execution_receipt_recorded",
    ]
    assert output["timeline"][0]["kind"] == "dispatch"
    assert output["receipt"]["usage"] == {"input_tokens": 12, "output_tokens": 7}
    assert output["receipt"]["actual_model"] == "gpt-5.6-sol-20260701"
    assert output["receipt"]["error"] is None
    assert output["provider_session_id"] == "provider-session:trace"
    assert output["provider_session_id_refs"][0]["cursor"] == 2


def test_trace_fails_fast_for_unknown_execution(monkeypatch, tmp_path) -> None:
    config = tmp_path / "vsm.toml"
    config.write_text("unused", encoding="utf-8")
    runtime = _runtime()
    monkeypatch.setattr("vsm.cli.bootstrap", lambda _config: runtime)

    result = CliRunner().invoke(
        app,
        ["trace", "execution:missing", "--config", str(config)],
    )

    assert result.exit_code != 0
    assert "execution_id not found: execution:missing" in (
        result.output + str(result.exception)
    )
