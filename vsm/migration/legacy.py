from __future__ import annotations

import hashlib
import json
import shutil
import stat
from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from vsm.errors import InvariantViolation
from vsm.ids import validate_id
from vsm.interface.models import (
    Commitment,
    Conversation,
    ConversationMessage,
    Decision,
    NodeMemory,
)
from vsm.kernel.ledger import OperationalLedger
from vsm.kernel.models import EventEnvelope


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LegacyKind(StrEnum):
    CONVERSATION = "conversation"
    DECISION = "decision"
    COMMITMENT = "commitment"
    NODE_MEMORY = "node_memory"


class SourceAssignment(StrictModel):
    owner_id: str
    interface_node_id: str
    conversation_id: str
    node_id: str
    owner_senders: tuple[str, ...]


class OwnershipAssignment(StrictModel):
    target_data_space_id: str
    sources: dict[str, SourceAssignment]


class LegacyFile(StrictModel):
    relative_path: str
    byte_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LegacyCensus(StrictModel):
    source_root: str
    file_count: int = Field(ge=0)
    byte_count: int = Field(ge=0)
    event_log_count: int = Field(ge=0)
    relevant_record_count: int = Field(ge=0)
    relevant_counts: dict[str, int]
    required_source_assignments: tuple[str, ...]
    files: tuple[LegacyFile, ...]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relevant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MigrationPlan(StrictModel):
    generated_at: datetime
    census: LegacyCensus
    assignment: OwnershipAssignment
    assignment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    import_event_count: int = Field(ge=0)


class ImportReceipt(StrictModel):
    imported_at: datetime
    source_manifest_sha256: str
    relevant_sha256: str
    assignment_sha256: str
    planned_event_count: int
    imported_event_count: int
    imported_by_kind: dict[str, int]


class LegacyRecord(StrictModel):
    kind: LegacyKind
    source: str
    source_file: str
    line_number: int = Field(gt=0)
    occurred_at: datetime
    raw: dict[str, object]

    @property
    def stable_key(self) -> str:
        return f"{self.source_file}:{self.line_number}:{self.kind}"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_name(relative_path: Path) -> str:
    if len(relative_path.parts) < 2:
        raise InvariantViolation(
            f"legacy file is not inside a source container: {relative_path}"
        )
    return relative_path.parts[0]


def _kind(event_type: str) -> LegacyKind | None:
    if event_type == "channel_message":
        return LegacyKind.CONVERSATION
    if event_type == "consortium_decided":
        return LegacyKind.DECISION
    if event_type == "task_submitted":
        return LegacyKind.COMMITMENT
    if event_type == "summary_generated":
        return LegacyKind.NODE_MEMORY
    return None


def _timestamp(raw: dict[str, object]) -> datetime:
    value = raw.get("ts")
    if not isinstance(value, str):
        raise InvariantViolation("legacy event timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise InvariantViolation("legacy event timestamp must include a timezone")
    return parsed.astimezone(UTC)


def scan_legacy(source_root: Path) -> tuple[LegacyCensus, tuple[LegacyRecord, ...]]:
    if not source_root.is_dir():
        raise InvariantViolation(f"legacy source directory not found: {source_root}")
    files: list[LegacyFile] = []
    records: list[LegacyRecord] = []
    event_logs = 0
    for path in sorted(item for item in source_root.rglob("*") if item.is_file()):
        relative = path.relative_to(source_root)
        data = path.read_bytes()
        files.append(
            LegacyFile(
                relative_path=relative.as_posix(),
                byte_count=len(data),
                sha256=_sha(data),
            )
        )
        if path.name != "events.jsonl":
            continue
        event_logs += 1
        for line_number, line in enumerate(data.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InvariantViolation(
                    f"invalid legacy JSON at {relative}:{line_number}"
                ) from exc
            if not isinstance(raw, dict):
                raise InvariantViolation(
                    f"legacy event is not an object at {relative}:{line_number}"
                )
            event_type = raw.get("event_type")
            kind = _kind(event_type) if isinstance(event_type, str) else None
            if kind is None:
                continue
            records.append(
                LegacyRecord(
                    kind=kind,
                    source=_source_name(relative),
                    source_file=relative.as_posix(),
                    line_number=line_number,
                    occurred_at=_timestamp(raw),
                    raw=raw,
                )
            )
    manifest_body = [item.model_dump(mode="json") for item in files]
    relevant_body = [
        {
            "kind": item.kind,
            "source": item.source,
            "source_file": item.source_file,
            "line_number": item.line_number,
            "raw_sha256": _sha(_canonical(item.raw)),
        }
        for item in records
    ]
    counts = Counter(item.kind.value for item in records)
    census = LegacyCensus(
        source_root=str(source_root.resolve()),
        file_count=len(files),
        byte_count=sum(item.byte_count for item in files),
        event_log_count=event_logs,
        relevant_record_count=len(records),
        relevant_counts=dict(sorted(counts.items())),
        required_source_assignments=tuple(sorted({item.source for item in records})),
        files=tuple(files),
        manifest_sha256=_sha(_canonical(manifest_body)),
        relevant_sha256=_sha(_canonical(relevant_body)),
    )
    return census, tuple(records)


def load_assignment(path: Path) -> OwnershipAssignment:
    if not path.is_file():
        raise InvariantViolation(f"ownership assignment not found: {path}")
    try:
        assignment = OwnershipAssignment.model_validate_json(path.read_text("utf-8"))
    except Exception as exc:
        raise InvariantViolation(f"invalid ownership assignment: {exc}") from exc
    validate_id(assignment.target_data_space_id)
    for source, item in assignment.sources.items():
        if not source or "/" in source or "\\" in source:
            raise InvariantViolation(f"invalid legacy source name: {source!r}")
        for value in (
            item.owner_id,
            item.interface_node_id,
            item.conversation_id,
            item.node_id,
        ):
            validate_id(value)
    return assignment


def build_plan(source_root: Path, assignment_path: Path) -> MigrationPlan:
    census, records = scan_legacy(source_root)
    assignment = load_assignment(assignment_path)
    missing = sorted(
        set(census.required_source_assignments) - set(assignment.sources)
    )
    extra = sorted(set(assignment.sources) - set(census.required_source_assignments))
    if missing or extra:
        raise InvariantViolation(
            "ownership assignment does not exactly cover legacy sources; "
            f"missing={missing}, extra={extra}"
        )
    assignment_sha = _sha(_canonical(assignment.model_dump(mode="json")))
    conversation_count = len({record.source for record in records})
    return MigrationPlan(
        generated_at=datetime.now(UTC),
        census=census,
        assignment=assignment,
        assignment_sha256=assignment_sha,
        import_event_count=conversation_count + len(records),
    )


def _stable_id(kind: str, stable_key: str) -> str:
    return f"{kind}:{_sha(stable_key.encode('utf-8'))[:32]}"


def _text(raw: dict[str, object], *keys: str) -> str:
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    nested = payload.get("payload")
    if isinstance(nested, str) and nested.strip():
        return nested
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _append(
    ledger: OperationalLedger,
    *,
    data_space_id: str,
    stream_id: str,
    versions: dict[str, int],
    event_type: str,
    occurred_at: datetime,
    actor_type: str,
    actor_id: str | None,
    stable_key: str,
    payload: dict[str, object],
) -> None:
    expected = versions.get(stream_id)
    if expected is None:
        existing = ledger.stream(stream_id, 0, 100_000)
        expected = existing[-1].event.stream_version if existing else 0
    event = EventEnvelope(
        event_id=_stable_id("event", stable_key),
        data_space_id=data_space_id,
        stream_id=stream_id,
        stream_version=expected + 1,
        event_type=event_type,
        occurred_at=occurred_at,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=stream_id,
        causation_id=None,
        idempotency_key=f"legacy:{_sha(stable_key.encode('utf-8'))}",
        payload=payload,
    )
    result = ledger.append(event, expected)
    versions[stream_id] = result.stream_version


def import_plan(
    plan: MigrationPlan,
    *,
    source_root: Path,
    ledger: OperationalLedger,
    data_space_id: str,
) -> ImportReceipt:
    census, records = scan_legacy(source_root)
    assignment_sha = _sha(_canonical(plan.assignment.model_dump(mode="json")))
    if census.manifest_sha256 != plan.census.manifest_sha256:
        raise InvariantViolation("legacy source manifest changed after dry-run")
    if census.relevant_sha256 != plan.census.relevant_sha256:
        raise InvariantViolation("legacy relevant record set changed after dry-run")
    if assignment_sha != plan.assignment_sha256:
        raise InvariantViolation("ownership assignment changed after dry-run")
    if plan.assignment.target_data_space_id != data_space_id:
        raise InvariantViolation("migration target DataSpace mismatch")

    versions: dict[str, int] = {}
    counts: Counter[str] = Counter()
    first_by_source: dict[str, datetime] = {}
    for record in records:
        first_by_source.setdefault(record.source, record.occurred_at)
    for source in sorted(first_by_source):
        assigned = plan.assignment.sources[source]
        conversation = Conversation(
            conversation_id=assigned.conversation_id,
            data_space_id=data_space_id,
            interface_node_id=assigned.interface_node_id,
            owner_id=assigned.owner_id,
            title=f"Imported history: {source}",
        )
        _append(
            ledger,
            data_space_id=data_space_id,
            stream_id=assigned.conversation_id,
            versions=versions,
            event_type="legacy_conversation_created",
            occurred_at=first_by_source[source],
            actor_type="human",
            actor_id=assigned.owner_id,
            stable_key=f"{source}:conversation",
            payload={"conversation": conversation.model_dump(mode="json")},
        )
        counts["conversation"] += 1

    for record in records:
        assigned = plan.assignment.sources[record.source]
        raw_bytes = _canonical(record.raw)
        blob_ref = ledger.put_blob(raw_bytes)
        if record.kind is LegacyKind.CONVERSATION:
            payload = record.raw.get("payload")
            sender = payload.get("sender") if isinstance(payload, dict) else None
            role = "owner" if sender in assigned.owner_senders else "interface"
            message = ConversationMessage(
                message_id=_stable_id("message", record.stable_key),
                conversation_id=assigned.conversation_id,
                role=role,
                display_text=None if role == "owner" else _text(record.raw),
                blob_ref=blob_ref if role == "owner" else None,
                occurred_at=record.occurred_at,
                source=None,
            )
            event_type = "legacy_conversation_message_imported"
            content = {"message": message.model_dump(mode="json"), "raw_blob_ref": blob_ref}
            stream_id = assigned.conversation_id
        elif record.kind is LegacyKind.DECISION:
            decision = Decision(
                decision_id=_stable_id("decision", record.stable_key),
                conversation_id=assigned.conversation_id,
                statement=_text(record.raw, "reason", "decision"),
                supersedes_decision_id=None,
            )
            event_type = "legacy_decision_imported"
            content = {"decision": decision.model_dump(mode="json"), "raw_blob_ref": blob_ref}
            stream_id = assigned.conversation_id
        elif record.kind is LegacyKind.COMMITMENT:
            commitment = Commitment(
                commitment_id=_stable_id("commitment", record.stable_key),
                conversation_id=assigned.conversation_id,
                statement=_text(record.raw, "description"),
                work_item_id=None,
                state="open",
            )
            event_type = "legacy_commitment_imported"
            content = {
                "commitment": commitment.model_dump(mode="json"),
                "raw_blob_ref": blob_ref,
            }
            stream_id = assigned.conversation_id
        else:
            memory = NodeMemory(
                memory_id=_stable_id("memory", record.stable_key),
                node_id=assigned.node_id,
                statement=_text(record.raw, "approach"),
                source_blob_ref=blob_ref,
            )
            event_type = "legacy_node_memory_imported"
            content = {"memory": memory.model_dump(mode="json")}
            stream_id = assigned.node_id
        _append(
            ledger,
            data_space_id=data_space_id,
            stream_id=stream_id,
            versions=versions,
            event_type=event_type,
            occurred_at=record.occurred_at,
            actor_type="system",
            actor_id=None,
            stable_key=record.stable_key,
            payload=content,
        )
        counts[record.kind.value] += 1

    imported = sum(counts.values())
    if imported != plan.import_event_count:
        raise InvariantViolation(
            f"import count differs from dry-run: planned={plan.import_event_count}, "
            f"actual={imported}"
        )
    return ImportReceipt(
        imported_at=datetime.now(UTC),
        source_manifest_sha256=census.manifest_sha256,
        relevant_sha256=census.relevant_sha256,
        assignment_sha256=assignment_sha,
        planned_event_count=plan.import_event_count,
        imported_event_count=imported,
        imported_by_kind=dict(sorted(counts.items())),
    )


def archive_legacy(
    source_root: Path, destination: Path, expected_manifest_sha256: str
) -> LegacyCensus:
    census, _ = scan_legacy(source_root)
    if census.manifest_sha256 != expected_manifest_sha256:
        raise InvariantViolation("legacy archive source differs from dry-run manifest")
    if destination.exists():
        raise InvariantViolation(f"legacy archive destination already exists: {destination}")
    destination.mkdir(parents=True)
    for item in census.files:
        source = source_root / Path(item.relative_path)
        target = destination / Path(item.relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if _sha(target.read_bytes()) != item.sha256:
            raise InvariantViolation(f"legacy archive copy digest mismatch: {item.relative_path}")
        target.chmod(stat.S_IREAD)
    manifest_path = destination / "legacy-manifest.json"
    manifest_path.write_text(census.model_dump_json(indent=2), "utf-8")
    manifest_path.chmod(stat.S_IREAD)
    for directory in sorted(
        (item for item in destination.rglob("*") if item.is_dir()), reverse=True
    ):
        directory.chmod(stat.S_IREAD | stat.S_IEXEC)
    return census
