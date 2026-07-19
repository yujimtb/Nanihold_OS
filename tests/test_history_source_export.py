from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools.history_source_export import (
    HistorySourceExportError,
    convert_intercom_export,
    convert_nanihold_legacy,
    convert_system_snapshot,
    write_export,
)


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_intercom_export(
    path: Path, records: list[dict[str, object]], *, ready: bool = True
) -> None:
    path.mkdir()
    payload = b"".join(canonical(record) + b"\n" for record in records)
    (path / "history.jsonl").write_bytes(payload)
    manifest = {
        "schema_version": 1,
        "source": "nanihold-intercom",
        "generated_at": "2026-07-20T01:00:00Z",
        "record_count": len(records),
        "export_sha256": sha(payload),
        "drain": {"ready_for_cutover": ready},
    }
    (path / "manifest.json").write_bytes(canonical(manifest))


def intercom_record(
    native_id: str, content: str, *, order: int, occurred_at: str | None
) -> dict[str, object]:
    payload = {
        "platform": "slack",
        "channel": "C1",
        "message_id": native_id,
        "author_id": "owner",
        "text": content,
        "reply_to_message_id": None,
    }
    return {
        "schema_version": 1,
        "source": "nanihold-intercom",
        "stream": "inbox",
        "source_native_id": f"slack:C1:{native_id}",
        "occurred_at": occurred_at,
        "author_id": "owner",
        "surface": "slack",
        "channel_id": "C1",
        "order": order,
        "content_sha256": sha(content.encode()),
        "payload_sha256": sha(canonical(payload)),
        "payload": payload,
    }


def test_intercom_keeps_identical_short_messages_with_different_native_ids(
    tmp_path: Path,
) -> None:
    export = tmp_path / "intercom"
    write_intercom_export(
        export,
        [
            intercom_record("m1", "はい", order=1, occurred_at="2026-07-20T00:00:00Z"),
            intercom_record("m2", "はい", order=2, occurred_at="2026-07-20T00:01:00Z"),
        ],
    )
    records, _ = convert_intercom_export(export, require_cutover_ready=True)
    assert len(records) == 2
    assert {record["source_message_id"] for record in records} == {
        "slack:C1:m1",
        "slack:C1:m2",
    }


def test_intercom_deduplicates_only_matching_native_identity_and_raw(
    tmp_path: Path,
) -> None:
    export = tmp_path / "intercom"
    first = intercom_record(
        "m1", "同一配送", order=1, occurred_at="2026-07-20T00:00:00Z"
    )
    write_intercom_export(export, [first, first])
    records, _ = convert_intercom_export(export, require_cutover_ready=True)
    assert len(records) == 1


def test_intercom_infers_only_missing_timestamp_from_signed_manifest(
    tmp_path: Path,
) -> None:
    export = tmp_path / "intercom"
    write_intercom_export(
        export,
        [intercom_record("m1", "再開", order=1, occurred_at=None)],
    )
    records, _ = convert_intercom_export(export, require_cutover_ready=True)
    assert records[0]["published_at"] == "2026-07-20T01:00:00Z"
    assert records[0]["metadata"]["timestamp_inferred_from"] == "manifest.generated_at"


def test_intercom_rejects_unready_cutover_and_digest_drift(tmp_path: Path) -> None:
    export = tmp_path / "intercom"
    write_intercom_export(
        export,
        [intercom_record("m1", "停止", order=1, occurred_at=None)],
        ready=False,
    )
    with pytest.raises(HistorySourceExportError, match="not ready"):
        convert_intercom_export(export, require_cutover_ready=True)
    (export / "history.jsonl").write_bytes(b"{}\n")
    with pytest.raises(HistorySourceExportError, match="digest"):
        convert_intercom_export(export, require_cutover_ready=False)


def write_legacy_event(
    path: Path, event_type: str, payload: dict[str, object], timestamp: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"event_type": event_type, "ts": timestamp, "payload": payload},
            ensure_ascii=False,
        )
        + "\n",
        "utf-8",
    )


def test_legacy_requires_exact_ownership_and_preserves_node_memory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "runs"
    write_legacy_event(
        source / "mission-a" / "events.jsonl",
        "summary_generated",
        {"approach": "長期記憶"},
        "2026-07-20T00:00:00Z",
    )
    assignment = tmp_path / "ownership.json"
    assignment.write_text(
        json.dumps(
            {
                "target_data_space_id": "data-space:personal",
                "sources": {},
            }
        ),
        "utf-8",
    )
    with pytest.raises(HistorySourceExportError, match="missing=\\['mission-a'\\]"):
        convert_nanihold_legacy(source, assignment)

    assignment.write_text(
        json.dumps(
            {
                "target_data_space_id": "data-space:personal",
                "sources": {
                    "mission-a": {
                        "owner_id": "human:owner",
                        "interface_node_id": "node:interface",
                        "conversation_id": "conversation:mission-a",
                        "node_id": "node:mission-a",
                        "owner_senders": ["owner"],
                    }
                },
            }
        ),
        "utf-8",
    )
    records, report = convert_nanihold_legacy(source, assignment)
    assert records[0]["record_kind"] == {
        "kind": "node_memory",
        "memory_id": records[0]["record_kind"]["memory_id"],
        "node_id": "node:mission-a",
    }
    assert report["owned_sources"] == ["mission-a"]


def test_system_snapshot_is_strict_and_atomic(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "captured_at": "2026-07-20T00:00:00Z",
                "source_instance_id": "desktop-primary",
                "states": [
                    {
                        "state_key": "git:nanihold",
                        "text": "branch=feature",
                        "value": {"branch": "feature", "dirty": False},
                    }
                ],
            }
        ),
        "utf-8",
    )
    records, report = convert_system_snapshot(snapshot)
    output = tmp_path / "output" / "snapshot.jsonl"
    report_path = tmp_path / "output" / "report.json"
    write_export(records, report, output, report_path)
    written = json.loads(output.read_text("utf-8"))
    assert written["record_kind"] == {
        "kind": "current_state",
        "state_key": "git:nanihold",
    }
    assert bytes(written["raw"]) == canonical(
        {
            "state_key": "git:nanihold",
            "text": "branch=feature",
            "value": {"branch": "feature", "dirty": False},
        }
    )
    with pytest.raises(HistorySourceExportError, match="must not already exist"):
        write_export(records, report, output, report_path)


def test_snapshot_rejects_implicit_fields(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "captured_at": datetime.now(UTC).isoformat(),
                "source_instance_id": "desktop-primary",
                "states": [],
                "secret": "must-not-be-accepted",
            }
        ),
        "utf-8",
    )
    with pytest.raises(HistorySourceExportError, match="requires exactly"):
        convert_system_snapshot(snapshot)
