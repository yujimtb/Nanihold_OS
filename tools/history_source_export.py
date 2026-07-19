"""Build strict LETHE HistoryRawRecord JSONL from non-native history sources.

This is a one-time migration producer. It is deliberately not imported by the
Nanihold runtime, and it never writes to LETHE directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from vsm.errors import InvariantViolation
from vsm.migration.legacy import (
    LegacyKind,
    OwnershipAssignment,
    load_assignment,
    scan_legacy,
)


class HistorySourceExportError(RuntimeError):
    """Raised when an input cannot be represented without guessing."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise HistorySourceExportError(f"{label} must be a non-empty ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistorySourceExportError(f"{label} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise HistorySourceExportError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        suffix = "a string" if allow_empty else "a non-empty string"
        raise HistorySourceExportError(f"{label} must be {suffix}")
    return value


def _record(
    *,
    source_session_id: str,
    source_message_id: str,
    parent_message_id: str | None,
    published_at: datetime,
    ordinal: int,
    author: str,
    surface: str,
    channel: str,
    text: str,
    record_kind: dict[str, object],
    raw: bytes,
    metadata: dict[str, str],
) -> dict[str, object]:
    if ordinal < 0:
        raise HistorySourceExportError("ordinal must be non-negative")
    return {
        "source_session_id": _string(source_session_id, "source_session_id"),
        "source_message_id": _string(source_message_id, "source_message_id"),
        "parent_message_id": parent_message_id,
        "published_at": _timestamp_text(published_at),
        "ordinal": ordinal,
        "author": _string(author, "author"),
        "surface": _string(surface, "surface"),
        "channel": _string(channel, "channel"),
        "text": _string(text, "text", allow_empty=True),
        "record_kind": record_kind,
        "raw": list(raw),
        "metadata": dict(sorted(metadata.items())),
    }


def _validate_unique_identity(
    records: Iterable[dict[str, object]],
) -> tuple[dict[str, object], ...]:
    accepted: list[dict[str, object]] = []
    identities: dict[tuple[str, str], str] = {}
    for record in records:
        identity = (
            str(record["source_session_id"]),
            str(record["source_message_id"]),
        )
        raw = bytes(record["raw"])  # type: ignore[arg-type]
        digest = _sha256(raw)
        previous = identities.get(identity)
        if previous is not None:
            if previous == digest:
                continue
            raise HistorySourceExportError(
                "source-native identity collision has different raw bytes: "
                f"{identity[0]}/{identity[1]}"
            )
        identities[identity] = digest
        accepted.append(record)
    return tuple(accepted)


def convert_intercom_export(
    export_dir: Path,
    *,
    require_cutover_ready: bool,
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    """Convert an atomic nanihold_intercom history export."""
    if not export_dir.is_absolute():
        raise HistorySourceExportError("Intercom export path must be absolute")
    history_path = export_dir / "history.jsonl"
    manifest_path = export_dir / "manifest.json"
    if not history_path.is_file() or not manifest_path.is_file():
        raise HistorySourceExportError(
            "Intercom export must contain history.jsonl and manifest.json"
        )
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HistorySourceExportError("Intercom manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("source") != "nanihold-intercom":
        raise HistorySourceExportError("Intercom manifest source is invalid")
    history_bytes = history_path.read_bytes()
    if _sha256(history_bytes) != manifest.get("export_sha256"):
        raise HistorySourceExportError("Intercom export digest differs from manifest")
    if require_cutover_ready:
        drain = manifest.get("drain")
        if not isinstance(drain, dict) or drain.get("ready_for_cutover") is not True:
            raise HistorySourceExportError("Intercom export is not ready for cutover")
    generated_at = _parse_timestamp(
        manifest.get("generated_at"), "Intercom manifest generated_at"
    )

    converted: list[dict[str, object]] = []
    for line_number, line in enumerate(history_bytes.splitlines(), start=1):
        if not line:
            raise HistorySourceExportError(
                f"blank Intercom history line at {line_number}"
            )
        try:
            source = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HistorySourceExportError(
                f"invalid Intercom history JSON at line {line_number}"
            ) from exc
        if not isinstance(source, dict):
            raise HistorySourceExportError(
                f"Intercom history row is not an object at line {line_number}"
            )
        raw = _canonical(source)
        payload = source.get("payload")
        if not isinstance(payload, dict):
            raise HistorySourceExportError(
                f"Intercom payload is not an object at line {line_number}"
            )
        if _sha256(_canonical(payload)) != source.get("payload_sha256"):
            raise HistorySourceExportError(
                f"Intercom payload digest differs at line {line_number}"
            )
        content = payload.get("text")
        if not isinstance(content, str):
            content = payload.get("response_text")
        if not isinstance(content, str):
            content = ""
        content_digest = source.get("content_sha256")
        if content_digest is not None and _sha256(content.encode("utf-8")) != content_digest:
            raise HistorySourceExportError(
                f"Intercom content digest differs at line {line_number}"
            )

        stream = _string(source.get("stream"), "Intercom stream")
        surface = _string(
            source.get("surface") or "intercom",
            "Intercom surface",
        )
        channel = _string(
            source.get("channel_id") or f"intercom:{stream}",
            "Intercom channel",
        )
        occurred_at_value = source.get("occurred_at")
        inferred_timestamp = occurred_at_value is None
        published_at = (
            generated_at
            if inferred_timestamp
            else _parse_timestamp(occurred_at_value, "Intercom occurred_at")
        )
        source_native_id = _string(
            source.get("source_native_id"), "Intercom source_native_id"
        )
        order = source.get("order")
        if not isinstance(order, int) or isinstance(order, bool) or order <= 0:
            raise HistorySourceExportError("Intercom order must be a positive integer")
        author_value = source.get("author_id")
        author = (
            _string(author_value, "Intercom author_id")
            if author_value is not None
            else "system:intercom"
        )
        parent_value = payload.get("reply_to_message_id")
        parent = (
            _string(parent_value, "Intercom reply_to_message_id")
            if parent_value is not None
            else None
        )

        if stream == "inbox" or (
            stream == "outbox"
            and isinstance(payload.get("response_text"), str)
            and payload.get("stage") == "completed"
        ):
            record_kind: dict[str, object] = {"kind": "message"}
        else:
            record_kind = {
                "kind": "current_state",
                "state_key": f"intercom:{stream}:{source_native_id}",
            }
        metadata = {
            "intercom_stream": stream,
            "payload_sha256": str(source["payload_sha256"]),
            "raw_sha256": _sha256(raw),
        }
        if inferred_timestamp:
            metadata["timestamp_inferred_from"] = "manifest.generated_at"
        converted.append(
            _record(
                source_session_id=f"{surface}:{channel}",
                source_message_id=source_native_id,
                parent_message_id=parent,
                published_at=published_at,
                ordinal=order,
                author=author,
                surface=surface,
                channel=channel,
                text=content,
                record_kind=record_kind,
                raw=raw,
                metadata=metadata,
            )
        )
    if len(converted) != manifest.get("record_count"):
        raise HistorySourceExportError(
            "Intercom converted record count differs from manifest"
        )
    return _validate_unique_identity(converted), manifest


def _legacy_text(raw: dict[str, object], *keys: str) -> str:
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    nested = payload.get("payload")
    if isinstance(nested, str):
        return nested
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def convert_nanihold_legacy(
    source_root: Path,
    assignment_path: Path,
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    """Convert the explicitly owned legacy Nanihold subset."""
    try:
        census, source_records = scan_legacy(source_root)
        assignment: OwnershipAssignment = load_assignment(assignment_path)
    except InvariantViolation as exc:
        raise HistorySourceExportError(str(exc)) from exc
    required = set(census.required_source_assignments)
    assigned = set(assignment.sources)
    missing = sorted(required - assigned)
    extra = sorted(assigned - required)
    if missing or extra:
        raise HistorySourceExportError(
            "ownership assignment must exactly cover legacy sources; "
            f"missing={missing}, extra={extra}"
        )

    records: list[dict[str, object]] = []
    ordinals: dict[str, int] = {}
    for source in source_records:
        assigned_source = assignment.sources[source.source]
        ordinal = ordinals.get(source.source, 0) + 1
        ordinals[source.source] = ordinal
        raw = _canonical(source.raw)
        native_id = f"{source.source_file}:{source.line_number}:{source.kind.value}"
        payload = source.raw.get("payload")
        sender = payload.get("sender") if isinstance(payload, dict) else None
        author = sender if isinstance(sender, str) and sender else "system:nanihold-legacy"
        if source.kind is LegacyKind.CONVERSATION:
            kind: dict[str, object] = {"kind": "message"}
            text = _legacy_text(source.raw)
        elif source.kind is LegacyKind.DECISION:
            decision_id = f"decision:{_sha256(native_id.encode())[:32]}"
            kind = {"kind": "decision", "decision_id": decision_id, "supersedes": []}
            text = _legacy_text(source.raw, "reason", "decision")
        elif source.kind is LegacyKind.COMMITMENT:
            commitment_id = f"commitment:{_sha256(native_id.encode())[:32]}"
            kind = {
                "kind": "commitment",
                "commitment_id": commitment_id,
                "status": "open",
            }
            text = _legacy_text(source.raw, "description")
        else:
            memory_id = f"memory:{_sha256(native_id.encode())[:32]}"
            kind = {
                "kind": "node_memory",
                "memory_id": memory_id,
                "node_id": assigned_source.node_id,
            }
            text = _legacy_text(source.raw, "approach")
        records.append(
            _record(
                source_session_id=assigned_source.conversation_id,
                source_message_id=native_id,
                parent_message_id=None,
                published_at=source.occurred_at,
                ordinal=ordinal,
                author=author,
                surface="nanihold_legacy",
                channel=source.source,
                text=text,
                record_kind=kind,
                raw=raw,
                metadata={
                    "legacy_source": source.source,
                    "source_file": source.source_file,
                    "line_number": str(source.line_number),
                    "raw_sha256": _sha256(raw),
                    "target_data_space_id": assignment.target_data_space_id,
                },
            )
        )
    report = {
        "source": "nanihold_legacy",
        "record_count": len(records),
        "manifest_sha256": census.manifest_sha256,
        "relevant_sha256": census.relevant_sha256,
        "target_data_space_id": assignment.target_data_space_id,
        "owned_sources": sorted(assigned),
    }
    return _validate_unique_identity(records), report


def convert_system_snapshot(
    snapshot_path: Path,
) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
    """Convert an explicit, secret-free current-state inventory."""
    if not snapshot_path.is_absolute() or not snapshot_path.is_file():
        raise HistorySourceExportError(
            "system snapshot path must be an existing absolute file"
        )
    raw_document = snapshot_path.read_bytes()
    try:
        document = json.loads(raw_document)
    except json.JSONDecodeError as exc:
        raise HistorySourceExportError("system snapshot is invalid JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "captured_at",
        "source_instance_id",
        "states",
    }:
        raise HistorySourceExportError(
            "system snapshot requires exactly captured_at, source_instance_id, states"
        )
    captured_at = _parse_timestamp(document["captured_at"], "snapshot captured_at")
    instance = _string(document["source_instance_id"], "snapshot source_instance_id")
    states = document["states"]
    if not isinstance(states, list):
        raise HistorySourceExportError("snapshot states must be a list")
    records: list[dict[str, object]] = []
    for ordinal, state in enumerate(states, start=1):
        if not isinstance(state, dict) or set(state) != {
            "state_key",
            "text",
            "value",
        }:
            raise HistorySourceExportError(
                "each snapshot state requires exactly state_key, text, value"
            )
        state_key = _string(state["state_key"], "snapshot state_key")
        text = _string(state["text"], "snapshot text", allow_empty=True)
        raw = _canonical(state)
        records.append(
            _record(
                source_session_id=f"system-snapshot:{instance}",
                source_message_id=f"{state_key}@{_timestamp_text(captured_at)}",
                parent_message_id=None,
                published_at=captured_at,
                ordinal=ordinal,
                author="system:nanihold",
                surface="system_snapshot",
                channel=instance,
                text=text,
                record_kind={"kind": "current_state", "state_key": state_key},
                raw=raw,
                metadata={"raw_sha256": _sha256(raw)},
            )
        )
    return _validate_unique_identity(records), {
        "source": "system_snapshot",
        "source_instance_id": instance,
        "captured_at": _timestamp_text(captured_at),
        "record_count": len(records),
        "source_sha256": _sha256(raw_document),
    }


def write_export(
    records: Sequence[dict[str, object]],
    report: dict[str, object],
    output_path: Path,
    report_path: Path,
) -> None:
    if not output_path.is_absolute() or not report_path.is_absolute():
        raise HistorySourceExportError("output and report paths must be absolute")
    if output_path.exists() or report_path.exists():
        raise HistorySourceExportError("output paths must not already exist")
    if output_path.parent != report_path.parent:
        raise HistorySourceExportError("output and report must share one directory")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(_canonical(record) + b"\n" for record in records)
    complete_report = {
        **report,
        "output_sha256": _sha256(payload),
        "output_record_count": len(records),
    }
    temporary = Path(
        tempfile.mkdtemp(prefix=".history-source-export-", dir=output_path.parent)
    )
    try:
        temporary_output = temporary / output_path.name
        temporary_report = temporary / report_path.name
        _write_fsynced(temporary_output, payload)
        _write_fsynced(temporary_report, _canonical(complete_report) + b"\n")
        os.replace(temporary_output, output_path)
        os.replace(temporary_report, report_path)
    except OSError as exc:
        raise HistorySourceExportError("failed to atomically write history export") from exc
    finally:
        try:
            temporary.rmdir()
        except OSError:
            pass


def _write_fsynced(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build strict LETHE HistoryRawRecord JSONL"
    )
    subcommands = parser.add_subparsers(dest="source", required=True)

    intercom = subcommands.add_parser("intercom")
    intercom.add_argument("--export-dir", type=Path, required=True)
    intercom.add_argument("--require-cutover-ready", action="store_true")

    legacy = subcommands.add_parser("nanihold-legacy")
    legacy.add_argument("--source-root", type=Path, required=True)
    legacy.add_argument("--assignment", type=Path, required=True)

    snapshot = subcommands.add_parser("system-snapshot")
    snapshot.add_argument("--snapshot", type=Path, required=True)

    for child in (intercom, legacy, snapshot):
        child.add_argument("--output", type=Path, required=True)
        child.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.source == "intercom":
            records, report = convert_intercom_export(
                arguments.export_dir.resolve(),
                require_cutover_ready=arguments.require_cutover_ready,
            )
        elif arguments.source == "nanihold-legacy":
            records, report = convert_nanihold_legacy(
                arguments.source_root.resolve(),
                arguments.assignment.resolve(),
            )
        else:
            records, report = convert_system_snapshot(arguments.snapshot.resolve())
        write_export(
            records,
            report,
            arguments.output.resolve(),
            arguments.report.resolve(),
        )
    except (HistorySourceExportError, OSError) as exc:
        print(f"history source export failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
