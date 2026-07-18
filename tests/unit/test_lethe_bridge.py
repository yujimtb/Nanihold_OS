"""Run 間会計・長期記憶 LETHE bridge の契約テスト。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from vsm.config import LetheConfig, load_config
from vsm.errors import ConfigError
from vsm.lethe_bridge import (
    SEARCH_V2_PATH,
    SUPPLEMENTAL_WRITE_PATH,
    HttpResponse,
    LetheBridge,
    LetheRequestError,
)
from vsm.lethe_bridge.models import (
    SUPPLEMENTAL_RECORD_ADAPTER,
    AccountingPayload,
    AccountingRecord,
    RunHeader,
    SearchResponse,
)


def _event(
    *,
    run_id: str,
    seq: int,
    event_type: str,
    payload: dict,
    node_id: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = "platform",
) -> dict:
    return {
        "event_id": f"event-{seq}",
        "seq": seq,
        "run_id": run_id,
        "node_id": node_id,
        "stream_id": node_id or run_id,
        "stream_version": seq + 1,
        "event_type": event_type,
        "schema_version": 1,
        "ts": f"2026-07-18T00:00:0{seq}.000Z",
        "actor_type": actor_type,
        "actor_id": actor_id,
        "correlation_id": run_id,
        "causation_id": None,
        "payload": payload,
    }


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
        encoding="utf-8",
    )


class RejectingTransport:
    def request(self, **_kwargs) -> HttpResponse:
        raise AssertionError("non-live bridge must not call transport")


def test_disabled_is_complete_no_op(tmp_path: Path) -> None:
    output = tmp_path / "must-not-exist.jsonl"
    bridge = LetheBridge(
        config=LetheConfig(enabled=False, dry_run_path=output),
        transport=RejectingTransport(),
    )

    assert bridge.search("") == []
    assert bridge.export_run(
        run_id="run-disabled",
        ended_at="2026-07-18T00:00:00.000Z",
        events_path=tmp_path / "missing-events.jsonl",
        nodes={},
        node_run_states={},
        run_consumption={},
    ) == []
    assert not output.exists()


@pytest.mark.asyncio
async def test_dry_run_exports_schema_and_injects_search_results(tmp_path: Path) -> None:
    run_id = "run-dry"
    events_path = tmp_path / "events.jsonl"
    output = tmp_path / "lethe.jsonl"
    events = [
        _event(
            run_id=run_id,
            seq=0,
            event_type="task_submitted",
            payload={
                "task_id": "task-1",
                "run_id": run_id,
                "description": "会計を分析する",
                "file_paths": [],
                "submitted_at": "2026-07-18T00:00:00.000Z",
            },
        ),
        _event(
            run_id=run_id,
            seq=1,
            event_type="policy_decision",
            payload={
                "decision_id": "decision-1",
                "assessment_id": "assessment-1",
                "directive": "監査所見を次回計画へ反映する",
                "followup_request": None,
            },
            node_id="node-s5",
            actor_type="agent",
            actor_id="agent-s5",
        ),
        _event(
            run_id=run_id,
            seq=2,
            event_type="audit_finding",
            payload={
                "finding_id": "finding-1",
                "s1_id": "node-s1",
                "content": "token消費の偏りを確認",
            },
            node_id="node-s3star",
            actor_type="agent",
            actor_id="agent-s3star",
        ),
        _event(
            run_id=run_id,
            seq=3,
            event_type="s1_completion",
            payload={"s1_id": "node-s1", "work_item_id": "work-1", "result": {}},
            node_id="node-s1",
        ),
    ]
    _write_events(events_path, events)
    nodes = {
        "node-s1": SimpleNamespace(vsm_position=SimpleNamespace(value="S1_WORKER"))
    }
    states = {
        (run_id, "node-s1"): SimpleNamespace(
            cost_consumed={
                "tokens_in": 10,
                "tokens_out": 5,
                "tokens_cache_read": 2,
                "tokens_total": 17,
                "wall_clock_ms": 120,
                "node_running_ms": 100,
            }
        )
    }
    bridge = LetheBridge(
        config=LetheConfig(enabled=True, mode="dry-run", dry_run_path=output),
        transport=RejectingTransport(),
    )

    exported = bridge.export_run(
        run_id=run_id,
        ended_at="2026-07-18T00:00:04.000Z",
        events_path=events_path,
        nodes=nodes,
        node_run_states=states,
        run_consumption={"tokens_total": 17},
    )

    assert [record.record_kind for record in exported] == [
        "run_accounting",
        "run_memory",
        "run_memory",
    ]
    assert exported[0].payload.result_state == "completed"
    assert exported[0].payload.node_consumption[0].consumed["tokens_total"] == 17
    stored = [
        SUPPLEMENTAL_RECORD_ADAPTER.validate_json(line)
        for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert stored == exported

    # 同じ Run の shutdown/export が再実行されても決定論的 record_id で重複しない。
    bridge.export_run(
        run_id=run_id,
        ended_at="2026-07-18T00:00:04.000Z",
        events_path=events_path,
        nodes=nodes,
        node_run_states=states,
        run_consumption={"tokens_total": 17},
    )
    assert len(output.read_text(encoding="utf-8").splitlines()) == 3

    injected: list = []
    found = await bridge.inject_context(
        "監査所見", lambda records: injected.extend(records)
    )
    assert [record.payload.event_type for record in found] == ["policy_decision"]
    assert injected == found


@dataclass
class MockLetheTransport:
    search_record: AccountingRecord
    requests: list[dict] = field(default_factory=list)

    def request(self, **kwargs) -> HttpResponse:
        self.requests.append(kwargs)
        if kwargs["method"] == "POST":
            return HttpResponse(status=201, body=b"")
        response = SearchResponse(records=[self.search_record])
        return HttpResponse(status=200, body=response.model_dump_json().encode("utf-8"))


def _accounting_record() -> AccountingRecord:
    run_id = "run-live"
    return AccountingRecord(
        record_id=f"nanihold:{run_id}:accounting",
        run_id=run_id,
        occurred_at="2026-07-18T00:00:01.000Z",
        text="Nanihold Run run-live result=completed",
        payload=AccountingPayload(
            header=RunHeader(
                run_id=run_id,
                started_at="2026-07-18T00:00:00.000Z",
                ended_at="2026-07-18T00:00:01.000Z",
            ),
            node_consumption=[],
            run_consumption={},
            result_state="completed",
            event_count=1,
        ),
    )


def test_live_uses_mock_supplemental_post_and_search_v2(tmp_path: Path) -> None:
    record = _accounting_record()
    transport = MockLetheTransport(search_record=record)
    bridge = LetheBridge(
        config=LetheConfig(
            enabled=True,
            mode="live",
            endpoint="https://lethe.invalid",
            token="test-token",
        ),
        transport=transport,
    )

    assert bridge.search("会計") == [record]
    events_path = tmp_path / "events.jsonl"
    _write_events(
        events_path,
        [
            _event(
                run_id="run-live",
                seq=0,
                event_type="s1_completion",
                payload={
                    "s1_id": "node-s1",
                    "work_item_id": "work-1",
                    "result": {},
                },
            )
        ],
    )
    bridge.export_run(
        run_id="run-live",
        ended_at="2026-07-18T00:00:01.000Z",
        events_path=events_path,
        nodes={},
        node_run_states={},
        run_consumption={},
    )

    search_request, write_request = transport.requests
    parsed_search = urlsplit(search_request["url"])
    assert search_request["method"] == "GET"
    assert parsed_search.path == SEARCH_V2_PATH
    assert parse_qs(parsed_search.query) == {"query": ["会計"]}
    assert search_request["headers"]["Authorization"] == "Bearer test-token"
    assert write_request["method"] == "POST"
    assert urlsplit(write_request["url"]).path == SUPPLEMENTAL_WRITE_PATH
    assert write_request["headers"]["Idempotency-Key"] == record.record_id
    assert json.loads(write_request["body"])["record_kind"] == "run_accounting"


def test_live_http_error_fails_without_fallback() -> None:
    class FailingTransport:
        def request(self, **_kwargs) -> HttpResponse:
            return HttpResponse(status=503, body=b'{"error":"unavailable"}')

    bridge = LetheBridge(
        config=LetheConfig(
            enabled=True,
            mode="live",
            endpoint="https://lethe.invalid",
            token="test-token",
        ),
        transport=FailingTransport(),
    )
    with pytest.raises(LetheRequestError, match="HTTP 503"):
        bridge.search("会計")


def test_lethe_toml_defaults_disabled_and_loads_explicit_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LITELLM_PROVIDER", raising=False)
    monkeypatch.chdir(tmp_path)
    assert load_config(None)[1].lethe == LetheConfig()

    config_path = tmp_path / "vsm.toml"
    config_path.write_text(
        "\n".join(
            [
                "[lethe]",
                "enabled = true",
                'mode = "dry-run"',
                'dry_run_path = "artifacts/lethe.jsonl"',
            ]
        ),
        encoding="utf-8",
    )
    lethe = load_config(config_path)[1].lethe
    assert lethe.enabled is True
    assert lethe.mode == "dry-run"
    assert lethe.dry_run_path == tmp_path / "artifacts" / "lethe.jsonl"


@pytest.mark.parametrize(
    "toml",
    [
        '[lethe]\nenabled = true\nmode = "live"\n',
        '[lethe]\nenabled = true\nmode = "live"\nendpoint = "https://lethe.invalid"\n',
        '[lethe]\nenabled = true\nmode = "live"\nendpoint = "not-a-url"\ntoken = "x"\n',
        '[lethe]\nenabled = true\nmode = "unknown"\n',
    ],
)
def test_invalid_lethe_config_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str
) -> None:
    monkeypatch.delenv("LITELLM_PROVIDER", raising=False)
    config_path = tmp_path / "vsm.toml"
    config_path.write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError, match="lethe|LETHE"):
        load_config(config_path)
