from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from vsm.errors import InvariantViolation
from vsm.kernel.ledger import InMemoryOperationalLedger
from vsm.migration.legacy import (
    archive_legacy,
    build_plan,
    import_plan,
    scan_legacy,
)


def event(event_type: str, payload: dict, minute: int) -> str:
    return json.dumps(
        {
            "ts": datetime(2026, 7, 1, 12, minute, tzinfo=UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            "event_type": event_type,
            "payload": payload,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def fixture(tmp_path):
    source = tmp_path / "source"
    first = source / "alpha"
    second = source / "beta"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "events.jsonl").write_text(
        "\n".join(
            (
                event(
                    "channel_message",
                    {"sender": "owner-a", "payload": "続けて"},
                    0,
                ),
                event("task_submitted", {"description": "finish gate"}, 1),
                event("summary_generated", {"approach": "persistent memory"}, 2),
            )
        ),
        "utf-8",
    )
    (second / "events.jsonl").write_text(
        event("consortium_decided", {"reason": "approved architecture"}, 3),
        "utf-8",
    )
    (second / "artifact.bin").write_bytes(b"artifact")
    assignment = tmp_path / "assignment.json"
    assignment.write_text(
        json.dumps(
            {
                "target_data_space_id": "space:personal",
                "sources": {
                    "alpha": {
                        "owner_id": "owner:primary",
                        "interface_node_id": "node:interface",
                        "conversation_id": "conversation:alpha",
                        "node_id": "node:interface",
                        "owner_senders": ["owner-a"],
                    },
                    "beta": {
                        "owner_id": "owner:primary",
                        "interface_node_id": "node:interface",
                        "conversation_id": "conversation:beta",
                        "node_id": "node:interface",
                        "owner_senders": [],
                    },
                },
            }
        ),
        "utf-8",
    )
    return source, assignment


def test_scan_lists_ambiguous_sources_and_import_matches_dry_run(tmp_path):
    source, assignment = fixture(tmp_path)
    census, records = scan_legacy(source)
    assert census.file_count == 3
    assert census.required_source_assignments == ("alpha", "beta")
    assert census.relevant_record_count == 4
    plan = build_plan(source, assignment)
    assert plan.import_event_count == 6
    ledger = InMemoryOperationalLedger("space:personal")
    receipt = import_plan(
        plan,
        source_root=source,
        ledger=ledger,
        data_space_id="space:personal",
    )
    assert receipt.imported_event_count == receipt.planned_event_count == 6
    assert receipt.source_manifest_sha256 == plan.census.manifest_sha256
    assert len(ledger.page(0, 100)) == 6


def test_source_change_after_dry_run_fails_and_archive_is_digest_fixed(tmp_path):
    source, assignment = fixture(tmp_path)
    plan = build_plan(source, assignment)
    (source / "beta" / "artifact.bin").write_bytes(b"changed")
    with pytest.raises(InvariantViolation, match="manifest changed"):
        import_plan(
            plan,
            source_root=source,
            ledger=InMemoryOperationalLedger("space:personal"),
            data_space_id="space:personal",
        )
    clean_source, clean_assignment = fixture(tmp_path / "clean")
    clean_plan = build_plan(clean_source, clean_assignment)
    destination = tmp_path / "archive"
    census = archive_legacy(
        clean_source, destination, clean_plan.census.manifest_sha256
    )
    assert (destination / "legacy-manifest.json").is_file()
    assert census.manifest_sha256 == clean_plan.census.manifest_sha256
    with pytest.raises(InvariantViolation, match="already exists"):
        archive_legacy(
            clean_source, destination, clean_plan.census.manifest_sha256
        )
