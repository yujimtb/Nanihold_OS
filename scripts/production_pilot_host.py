from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import tomllib
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Mapping, TypeAlias
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from vsm.environment import EnvironmentContract, environment_fingerprint
from vsm.environment.artifacts import LocalEnvironmentContractStore
from vsm.environment_instance import EnvironmentInstance, EnvironmentInstanceService
from vsm.interface.models import (
    InterfaceAction,
    ReadHistoryAction,
    SubmitReorientationAction,
)
from vsm.kernel.models import WorkState
from vsm.kernel.service import utc_now
from vsm.lethe.client import LetheOperationalLedger
from vsm.pilot.models import DeviceIdentity, ModelCandidate
from vsm.preflight import (
    DeclarationUpdateEvent,
    PreflightEvidence,
    PreflightGate,
    PreflightObservation,
    PreflightRunner,
    PreflightContractError,
    PreflightError,
    VerificationTuple,
    CliVersionReader,
)


CLAUDE_EFFORT = "high"
MAX_REQUEST_BYTES = 2_000_000
SHA256_PATTERN = r"^[0-9a-f]{64}$"
MCP_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
MCP_TOOL_PATTERN = re.compile(r"^mcp__([a-z][a-z0-9_-]{0,62})__[A-Za-z0-9_.-]+$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventDelta(StrictModel):
    after_cursor: Annotated[int, Field(ge=0)]
    through_cursor: Annotated[int, Field(ge=0)]
    event_count: Annotated[int, Field(ge=0)]
    event_type_counts: dict[str, Annotated[int, Field(ge=0)]]
    changed_stream_ids: tuple[str, ...]

    @model_validator(mode="after")
    def cursor_and_count_are_consistent(self) -> "EventDelta":
        if self.through_cursor < self.after_cursor:
            raise ValueError("event delta cursor regressed")
        if sum(self.event_type_counts.values()) != self.event_count:
            raise ValueError("event delta count differs from event_type_counts")
        return self


class ResumeReferencePack(StrictModel):
    node_memory_refs: tuple[str, ...]
    unfinished_work_item_ids: tuple[str, ...]
    open_commitment_ids: tuple[str, ...]
    active_decision_ids: tuple[str, ...]


class ClaudeTurnRequest(StrictModel):
    receipt_id: Annotated[str, Field(min_length=1)]
    idempotency_key: Annotated[str, Field(min_length=1)]
    device_identity: DeviceIdentity
    candidate: ModelCandidate
    permission_mode: Literal[
        "sandboxed_bypass", "managed_permissions", "observe_only"
    ]
    max_budget_usd: Annotated[float, Field(gt=0)]
    timeout_seconds: Annotated[float, Field(gt=0)]
    root_session_id: Annotated[str, Field(min_length=1)] | None
    fork_session: bool
    event_delta: EventDelta
    resume_pack: ResumeReferencePack | None

    @model_validator(mode="after")
    def session_request_is_safe(self) -> "ClaudeTurnRequest":
        if self.root_session_id is None and self.fork_session:
            raise ValueError("fork_session requires root_session_id")
        if self.root_session_id is not None and not self.fork_session:
            raise ValueError("root resume must fork the session")
        if self.root_session_id is not None and self.resume_pack is not None:
            raise ValueError("resumed session accepts event delta, not resume_pack")
        return self


class InterfaceTurnRequest(ClaudeTurnRequest):
    owner_text: Annotated[str, Field(min_length=1, max_length=200_000)]


class ReorientationHistoryResult(StrictModel):
    action_id: Annotated[str, Field(min_length=1)]
    operation: Annotated[str, Field(min_length=1)]
    result_json: Any
    result_blob_ref: Annotated[str, Field(min_length=1)]
    result_sha256: Annotated[str, Field(pattern=SHA256_PATTERN)]
    next_cursor: Annotated[str, Field(min_length=1)] | None
    source_cursor: Annotated[str, Field(min_length=1)]
    result_event_id: Annotated[str, Field(min_length=1)]
    event_cursor: Annotated[int, Field(gt=0)]


class ReorientationWorkItemSummary(StrictModel):
    work_item_id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    description: Annotated[str, Field(min_length=1)]
    acceptance_criteria: tuple[Annotated[str, Field(min_length=1)], ...]
    state: WorkState


class ReorientationAssessmentContract(StrictModel):
    import_id: Annotated[str, Field(min_length=1)]
    canonical_conversation_id: Annotated[str, Field(min_length=1)]
    covered_session_index_ref: Annotated[str, Field(min_length=1)]
    covered_session_count: Annotated[int, Field(ge=0)]
    open_commitment_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    resume_work_items: tuple[ReorientationWorkItemSummary, ...]
    minimum_history_cursor: Annotated[int, Field(ge=0)]


class ReorientationAssessmentContractReference(StrictModel):
    import_id: Annotated[str, Field(min_length=1)]
    canonical_conversation_id: Annotated[str, Field(min_length=1)]
    contract_sha256: Annotated[str, Field(pattern=SHA256_PATTERN)]
    covered_session_index_ref: Annotated[str, Field(min_length=1)]
    covered_session_count: Annotated[int, Field(ge=0)]
    open_commitment_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    resume_work_items: tuple[ReorientationWorkItemSummary, ...]
    minimum_history_cursor: Annotated[int, Field(ge=0)]


class SessionIndexSummary(StrictModel):
    session_count: Annotated[int, Field(ge=0)]
    source_kind_counts: dict[str, Annotated[int, Field(ge=0)]]
    first_message_at: Annotated[str, Field(min_length=1)] | None
    last_message_at: Annotated[str, Field(min_length=1)] | None


class ReorientationTurnRequest(ClaudeTurnRequest):
    objective: Annotated[str, Field(min_length=1, max_length=20_000)]
    session_index_ref: Annotated[str, Field(min_length=1)]
    open_commitment_refs: tuple[str, ...]
    current_state_ref: Annotated[str, Field(min_length=1)]
    history_result: ReorientationHistoryResult
    assessment_contract: (
        ReorientationAssessmentContract | ReorientationAssessmentContractReference
    )
    audited_history_event_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    assessment_contract_included: bool
    session_index_event_ids: tuple[Annotated[str, Field(min_length=1)], ...]
    session_index_summary: SessionIndexSummary

    @model_validator(mode="after")
    def contract_is_sent_only_on_initial_turn(self) -> "ReorientationTurnRequest":
        is_full = isinstance(self.assessment_contract, ReorientationAssessmentContract)
        if self.root_session_id is None and (not is_full or not self.assessment_contract_included):
            raise ValueError("initial reorientation turn requires the full assessment contract")
        if self.root_session_id is not None and (is_full or self.assessment_contract_included):
            raise ValueError("resumed reorientation turn requires only a contract reference")
        return self


class ArtifactRef(StrictModel):
    artifact_id: Annotated[str, Field(min_length=1)]
    sha256: Annotated[str, Field(pattern=SHA256_PATTERN)]
    media_type: Annotated[str, Field(min_length=1)]


class WorkItemHandoff(StrictModel):
    work_item_id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1, max_length=2_000)]
    objective: Annotated[str, Field(min_length=1, max_length=100_000)]
    agent_name: Annotated[str, Field(min_length=1)]


class TokenBudget(StrictModel):
    max_input_tokens: Annotated[int, Field(gt=0)]
    max_output_tokens: Annotated[int, Field(gt=0)]
    max_total_tokens: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def total_covers_parts(self) -> "TokenBudget":
        if self.max_total_tokens < self.max_input_tokens + self.max_output_tokens:
            raise ValueError("max_total_tokens must cover input and output limits")
        return self


class WorkExecutionRequest(StrictModel):
    receipt_id: Annotated[str, Field(min_length=1)]
    idempotency_key: Annotated[str, Field(min_length=1)]
    device_identity: DeviceIdentity
    candidate: ModelCandidate
    execution_id: Annotated[str, Field(min_length=1)]
    work_item: WorkItemHandoff
    unmet_acceptance: tuple[Annotated[str, Field(min_length=1)], ...]
    event_delta: EventDelta
    artifact_refs: tuple[ArtifactRef, ...]
    cwd: Annotated[str, Field(min_length=1)]
    sandbox: Literal["read-only", "workspace-write"]
    token_budget: TokenBudget
    timeout_seconds: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def acceptance_and_artifacts_are_unique(self) -> "WorkExecutionRequest":
        if not self.unmet_acceptance:
            raise ValueError("work execution requires unmet acceptance criteria")
        artifact_ids = [item.artifact_id for item in self.artifact_refs]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact_refs must be unique")
        return self


class StructuredInterfaceOutput(StrictModel):
    display_text: Annotated[str, Field(min_length=1, max_length=1_200)]
    actions: tuple[InterfaceAction, ...]


ReorientationAction: TypeAlias = Annotated[
    ReadHistoryAction | SubmitReorientationAction,
    Field(discriminator="kind"),
]


class StructuredReorientationOutput(StrictModel):
    display_text: Annotated[str, Field(min_length=1, max_length=1_200)]
    actions: Annotated[
        tuple[ReorientationAction, ...],
        Field(min_length=1, max_length=1),
    ]


class AcceptanceResult(StrictModel):
    criterion: Annotated[str, Field(min_length=1)]
    satisfied: bool
    evidence_refs: tuple[str, ...]


class StructuredWorkOutput(StrictModel):
    summary: Annotated[str, Field(min_length=1)]
    acceptance_results: tuple[AcceptanceResult, ...]
    artifact_refs: tuple[str, ...]
    event_notes: tuple[str, ...]
    completed: bool


def _claude_supported_json_schema(model: type[BaseModel]) -> dict[str, object]:
    def without_discriminator(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: without_discriminator(item)
                for key, item in value.items()
                if key != "discriminator"
            }
        if isinstance(value, list):
            return [without_discriminator(item) for item in value]
        return value

    schema = without_discriminator(model.model_json_schema())
    if not isinstance(schema, dict):
        raise RuntimeError("Claude structured output schema must be an object")
    return schema


INTERFACE_SCHEMA = _claude_supported_json_schema(StructuredInterfaceOutput)
REORIENTATION_SCHEMA = _claude_supported_json_schema(
    StructuredReorientationOutput
)
WORK_SCHEMA = StructuredWorkOutput.model_json_schema()


class ContractError(RuntimeError):
    pass


class ConflictError(RuntimeError):
    pass


class ProviderInvocationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        usage: dict[str, object] | None = None,
        actual_model: str | None = None,
        provider_session_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.usage = usage
        self.actual_model = actual_model
        self.provider_session_id = provider_session_id


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _request_sha256(request: BaseModel) -> str:
    return _sha256_json(
        request.model_dump(mode="json", exclude_computed_fields=True)
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _exact_fields(
    value: object,
    fields: set[str],
    label: str,
    *,
    optional_fields: set[str] | None = None,
) -> dict[str, Any]:
    allowed = fields | (optional_fields or set())
    if (
        not isinstance(value, dict)
        or not fields.issubset(value)
        or not set(value).issubset(allowed)
    ):
        raise RuntimeError(f"{label} fields differ from the exact contract")
    return value


def _candidate_key(candidate: ModelCandidate) -> str:
    return candidate.key


def _nested_string(value: object, key: str) -> str | None:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        for child in value.values():
            found = _nested_string(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _nested_string(child, key)
            if found is not None:
                return found
    return None


class ReceiptStore:
    """Durable idempotency and transport-unknown reconciliation boundary."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    endpoint TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            rows = connection.execute(
                "SELECT receipt_id, body_json FROM receipts WHERE status = 'in_progress'"
            ).fetchall()
            for receipt_id, body_json in rows:
                body = json.loads(body_json)
                body.update(
                    {
                        "status": "transport_unknown",
                        "error": {
                            "code": "TransportUnknown",
                            "message": (
                                "PilotHost restarted before the provider outcome "
                                "was durably recorded; reconciliation is required"
                            ),
                        },
                        "updated_at": _utc_now(),
                    }
                )
                connection.execute(
                    """
                    UPDATE receipts
                    SET status = 'transport_unknown', body_json = ?, updated_at = ?
                    WHERE receipt_id = ?
                    """,
                    (_canonical_json(body), body["updated_at"], receipt_id),
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def begin(
        self,
        *,
        endpoint: str,
        receipt_id: str,
        idempotency_key: str,
        request_sha256: str,
        candidate_key: str,
        requested_model: str | None,
    ) -> tuple[dict[str, object], bool]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT endpoint, receipt_id, request_sha256, body_json
                FROM receipts
                WHERE receipt_id = ? OR idempotency_key = ?
                """,
                (receipt_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                (
                    existing_endpoint,
                    existing_receipt_id,
                    existing_digest,
                    body_json,
                ) = existing
                if (
                    existing_endpoint != endpoint
                    or existing_receipt_id != receipt_id
                    or existing_digest != request_sha256
                ):
                    raise ConflictError(
                        "receipt or idempotency key was reused with a different request"
                    )
                return json.loads(body_json), False

            timestamp = _utc_now()
            body: dict[str, object] = {
                "receipt_id": receipt_id,
                "endpoint": endpoint,
                "idempotency_key": idempotency_key,
                "request_sha256": request_sha256,
                "status": "in_progress",
                "candidate_key": candidate_key,
                "requested_model": requested_model,
                "actual_model": None,
                "provider_session_id": None,
                "usage": None,
                "result": None,
                "error": None,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            connection.execute(
                """
                INSERT INTO receipts (
                    receipt_id, endpoint, idempotency_key, request_sha256,
                    status, body_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'in_progress', ?, ?, ?)
                """,
                (
                    receipt_id,
                    endpoint,
                    idempotency_key,
                    request_sha256,
                    _canonical_json(body),
                    timestamp,
                    timestamp,
                ),
            )
            return body, True

    def finish(
        self,
        receipt_id: str,
        *,
        status: Literal["succeeded", "failed"],
        actual_model: str | None,
        provider_session_id: str | None,
        usage: dict[str, object] | None,
        result: dict[str, object] | None,
        error: dict[str, str] | None,
    ) -> dict[str, object]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT body_json, status FROM receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("receipt disappeared during provider invocation")
            if row[1] != "in_progress":
                raise ConflictError("only an in-progress receipt can be completed")
            body = json.loads(row[0])
            body.update(
                {
                    "status": status,
                    "actual_model": actual_model,
                    "provider_session_id": provider_session_id,
                    "usage": usage,
                    "result": result,
                    "error": error,
                    "updated_at": _utc_now(),
                }
            )
            connection.execute(
                """
                UPDATE receipts
                SET status = ?, body_json = ?, updated_at = ?
                WHERE receipt_id = ?
                """,
                (status, _canonical_json(body), body["updated_at"], receipt_id),
            )
            return body

    def get(self, receipt_id: str) -> dict[str, object] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT body_json FROM receipts WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()
            return None if row is None else json.loads(row[0])


class McpAllowlist:
    def __init__(self, raw: object) -> None:
        data = _exact_fields(raw, {"allowlist", "servers"}, "MCP")
        allowlist = data["allowlist"]
        servers = data["servers"]
        if (
            not isinstance(allowlist, list)
            or not all(isinstance(item, str) for item in allowlist)
            or len(allowlist) != len(set(allowlist))
        ):
            raise RuntimeError("MCP allowlist must contain unique server names")
        if not isinstance(servers, dict) or set(servers) != set(allowlist):
            raise RuntimeError("MCP servers must exactly match the allowlist")
        self.servers: dict[str, dict[str, str]] = {}
        for name in allowlist:
            if MCP_NAME_PATTERN.fullmatch(name) is None:
                raise RuntimeError(f"invalid MCP server name: {name}")
            server = _exact_fields(
                servers[name],
                {"url", "bearer_token_env_var"},
                f"MCP server {name}",
            )
            url = server["url"]
            env_name = server["bearer_token_env_var"]
            if not isinstance(url, str) or not isinstance(env_name, str):
                raise RuntimeError("MCP URL and bearer token env name must be strings")
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise RuntimeError(f"MCP server URL is invalid: {name}")
            _required_env(env_name)
            self.servers[name] = {
                "url": url,
                "bearer_token_env_var": env_name,
            }

    def validate_candidate_tools(self, candidate: ModelCandidate) -> None:
        for tool in candidate.toolset:
            match = MCP_TOOL_PATTERN.fullmatch(tool)
            if match is None or match.group(1) not in self.servers:
                raise RuntimeError(
                    "candidate toolset must contain only tools from MCP allowlist"
                )

    def claude_json(self) -> str:
        mcp_servers = {
            name: {
                "type": "http",
                "url": server["url"],
                "headers": {
                    "Authorization": (
                        f"Bearer ${{{server['bearer_token_env_var']}}}"
                    )
                },
            }
            for name, server in self.servers.items()
        }
        return _canonical_json({"mcpServers": mcp_servers})

    def codex_config_arguments(self) -> list[str]:
        server_entries: list[str] = []
        for name, server in sorted(self.servers.items()):
            server_entries.append(
                f"{name}={{"
                f"url={json.dumps(server['url'])},"
                "bearer_token_env_var="
                f"{json.dumps(server['bearer_token_env_var'])}"
                "}"
            )
        # Override the complete root table so ambient user/project MCP entries cannot
        # be merged into the production allowlist.
        return ["-c", f"mcp_servers={{{','.join(server_entries)}}}"]


class ClaudeAdapter:
    def __init__(self, raw: object) -> None:
        data = _exact_fields(
            raw,
            {
                "candidate",
                "executable",
                "cli_version",
                "working_directory",
                "request_document_directory",
                "max_request_document_bytes",
                "permission_mode",
                "sandbox_profile_certificate_sha256",
                "mcp",
                "max_budget_usd",
                "timeout_seconds",
            },
            "Claude adapter",
        )
        self.candidate = ModelCandidate.model_validate(data["candidate"])
        if (
            self.candidate.adapter != "claude-code"
            or self.candidate.provider != "anthropic"
            or self.candidate.selection != "provider_configured"
            or self.candidate.model_snapshot is not None
            or self.candidate.effort != CLAUDE_EFFORT
        ):
            raise RuntimeError(
                "Claude Interface candidate must be claude-code/anthropic/high"
            )
        self.executable = _nonblank(data["executable"], "Claude executable")
        self.cli_version = _nonblank(data["cli_version"], "Claude CLI version")
        self.working_directory = Path(
            _nonblank(data["working_directory"], "Claude working directory")
        ).resolve()
        if not self.working_directory.is_dir():
            raise RuntimeError("Claude working directory does not exist")
        request_document_directory = Path(
            _nonblank(
                data["request_document_directory"],
                "Claude request document directory",
            )
        )
        if not request_document_directory.is_absolute():
            raise RuntimeError(
                "Claude request document directory must be absolute"
            )
        self.request_document_directory = request_document_directory.resolve()
        self.request_document_directory.mkdir(parents=True, exist_ok=True)
        if not self.request_document_directory.is_dir():
            raise RuntimeError("Claude request document directory is not a directory")
        self.provider_io_document_directory = (
            self.request_document_directory / "provider-io"
        )
        self.provider_io_document_directory.mkdir(parents=False, exist_ok=True)
        if not self.provider_io_document_directory.is_dir():
            raise RuntimeError("Claude provider I/O directory is not a directory")
        self.max_request_document_bytes = _positive_integer(
            data["max_request_document_bytes"],
            "Claude max request document bytes",
        )
        self.permission_mode = _nonblank(
            data["permission_mode"], "Claude permission mode"
        )
        if self.permission_mode not in {
            "sandboxed_bypass",
            "managed_permissions",
            "observe_only",
        }:
            raise RuntimeError("Claude permission mode is unsupported")
        certificate = data["sandbox_profile_certificate_sha256"]
        if self.permission_mode == "sandboxed_bypass":
            if (
                not isinstance(certificate, str)
                or re.fullmatch(SHA256_PATTERN, certificate) is None
            ):
                raise RuntimeError(
                    "sandboxed_bypass requires a SandboxProfile certificate"
                )
        elif certificate is not None:
            raise RuntimeError(
                "SandboxProfile certificate is valid only for sandboxed_bypass"
            )
        self.sandbox_profile_certificate_sha256 = certificate
        self.mcp = McpAllowlist(data["mcp"])
        self.mcp.validate_candidate_tools(self.candidate)
        self.max_budget_usd = _positive_number(
            data["max_budget_usd"], "Claude max budget"
        )
        self.timeout_seconds = _positive_number(
            data["timeout_seconds"], "Claude timeout"
        )

    def run_preflight(self, verification_tuple: VerificationTuple) -> PreflightObservation:
        """Run one non-mutating Claude Code trial for environment verification."""

        if verification_tuple.adapter != self.candidate.adapter:
            raise PreflightError("Claude preflight received a different adapter")
        permission_flag = {
            "sandboxed_bypass": "bypassPermissions",
            "managed_permissions": "auto",
            "observe_only": "plan",
        }[self.permission_mode]
        argv = [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            permission_flag,
            "--tools",
            "",
            "--disable-slash-commands",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config",
            self.mcp.claude_json(),
            "--allowedTools",
            "",
            "--max-budget-usd",
            str(self.max_budget_usd),
        ]
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            input=(
                "Nanihold execution-environment preflight. Do not modify the "
                "workspace. Return a short acknowledgement only."
            ),
            timeout=self.timeout_seconds,
            shell=False,
            cwd=self.working_directory,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise PreflightError("Claude preflight trial exited unsuccessfully")
        try:
            result = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise PreflightError("Claude preflight returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise PreflightError("Claude preflight returned a non-object response")
        sandbox_policy = (
            "workspace-write"
            if self.permission_mode == "sandboxed_bypass"
            else "read-only"
        )
        return PreflightObservation(
            sandbox_policy=sandbox_policy,
            capabilities={
                "workspace_writable": sandbox_policy == "workspace-write",
            },
            rollout_ref=None,
        )

    def invoke(
        self,
        endpoint: str,
        request: InterfaceTurnRequest | ReorientationTurnRequest,
    ) -> tuple[dict[str, object], str, dict[str, object], str]:
        self.validate_request(request)
        prompt_payload = self._request_document_payload(endpoint, request)

        (
            request_document,
            request_document_sha256,
            request_document_bytes,
        ) = self._write_request_document(prompt_payload)
        short_instruction = (
            "Read the appended Nanihold request document. Verify its SHA-256 is "
            f"{request_document_sha256}. Execute that exact contract and return "
            "one structured response."
        )
        if len(short_instruction.encode("utf-8")) > 256:
            raise RuntimeError("Claude stdio instruction exceeds the fixed short limit")

        permission_flag = {
            "sandboxed_bypass": "bypassPermissions",
            "managed_permissions": "auto",
            "observe_only": "plan",
        }[self.permission_mode]
        response_schema = (
            REORIENTATION_SCHEMA
            if endpoint == "/v1/reorientation-turn"
            else INTERFACE_SCHEMA
        )
        argv = [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            _canonical_json(response_schema),
            "--append-system-prompt-file",
            str(request_document),
            "--effort",
            self.candidate.effort,
            "--permission-mode",
            permission_flag,
            "--tools",
            "",
            "--disable-slash-commands",
            "--no-chrome",
            "--strict-mcp-config",
            "--mcp-config",
            self.mcp.claude_json(),
            "--allowedTools",
            ",".join(self.candidate.toolset),
            "--max-budget-usd",
            str(request.max_budget_usd),
        ]
        if request.root_session_id is not None:
            argv.extend(
                (
                    "--resume",
                    request.root_session_id,
                    "--fork-session",
                )
            )
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            input=short_instruction,
            timeout=request.timeout_seconds,
            shell=False,
            cwd=self.working_directory,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _, provider_io_sha256 = self._write_content_addressed_document(
            self.provider_io_document_directory,
            {
                "document_schema": "nanihold.provider-io-document",
                "document_schema_version": "1.0.0",
                "request_receipt_id": request.receipt_id,
                "request_sha256": _request_sha256(request),
                "request_document_sha256": request_document_sha256,
                "request_document_bytes": request_document_bytes,
                "endpoint": endpoint,
                "return_code": completed.returncode,
                "stdout_sha256": hashlib.sha256(
                    completed.stdout.encode("utf-8")
                ).hexdigest(),
                "stdout_bytes": len(completed.stdout.encode("utf-8")),
                "stdout_text": completed.stdout,
                "stderr_sha256": hashlib.sha256(
                    completed.stderr.encode("utf-8")
                ).hexdigest(),
                "stderr_bytes": len(completed.stderr.encode("utf-8")),
                "stderr_text": completed.stderr,
            },
        )
        return self._parse(completed, endpoint, provider_io_sha256)

    def _request_document_payload(
        self,
        endpoint: str,
        request: InterfaceTurnRequest | ReorientationTurnRequest,
    ) -> dict[str, object]:
        output_contract = (
            "Return one StructuredReorientationOutput with exactly one action. "
            "The action must be history.read or reorientation.submit. Keep display "
            "text concise and keep assessment prose within the schema bounds."
            if endpoint == "/v1/reorientation-turn"
            else "Return one StructuredInterfaceOutput."
        )
        prompt_payload: dict[str, object] = {
            "document_schema": "nanihold.interface-request-document",
            "document_schema_version": "1.0.0",
            "request_receipt_id": request.receipt_id,
            "request_idempotency_key": request.idempotency_key,
            "request_sha256": _request_sha256(request),
            "contract": (
                f"{output_contract} You are the Interface Pilot, "
                "not the persistent owner of memory. Use only typed MCP tools. Never "
                "claim an effect or completion that is absent from supplied evidence."
            ),
            "endpoint": endpoint,
            "event_delta": request.event_delta.model_dump(mode="json"),
            "resume_pack": (
                None
                if request.resume_pack is None
                else request.resume_pack.model_dump(mode="json")
            ),
        }
        if isinstance(request, InterfaceTurnRequest):
            prompt_payload["owner_text"] = request.owner_text
        else:
            prompt_payload.update(
                {
                    "objective": request.objective,
                    "session_index_ref": request.session_index_ref,
                    "open_commitment_refs": list(request.open_commitment_refs),
                    "current_state_ref": request.current_state_ref,
                    "history_result": request.history_result.model_dump(mode="json"),
                    "assessment_contract": request.assessment_contract.model_dump(mode="json"),
                    "audited_history_event_ids": list(
                        request.audited_history_event_ids
                    ),
                    "session_index_event_ids": list(request.session_index_event_ids),
                    "session_index_summary": request.session_index_summary.model_dump(mode="json"),
                    "assessment_submission_contract": (
                        "history_result is already the completed result of its "
                        "operation, argument, and page cursor. Never request that "
                        "same triple again. When next_cursor is non-null and another "
                        "page is needed, copy next_cursor exactly into page_cursor. "
                        "request_receipt_id, request_idempotency_key, request_sha256, "
                        "and document identifiers are transport audit metadata, not "
                        "history references; never pass them to resolve_reference. "
                        "A resolve_reference argument must be an explicit history "
                        "reference supplied in history_result or the assessment "
                        "contract, never an invented identifier. "
                        "A get_current_state result without an argument is a "
                        "paginated index without value bodies. Continue that index "
                        "with next_cursor only when needed, or request one value by "
                        "setting argument to an exact state_key from the index and "
                        "page_cursor to null. "
                        "For reorientation.submit, copy import_id and "
                        "canonical_conversation_id exactly from assessment_contract; "
                        "covered_session_index_ref and covered_session_count must be copied "
                        "exactly from assessment_contract; open_commitment_ids must be the exact "
                        "listed set; resume_work_item_ids must be selected only from "
                        "assessment_contract.resume_work_items[].work_item_id. The "
                        "title, description, acceptance_criteria, and state in each "
                        "resume_work_items entry are the verified compact WorkItem "
                        "summary; use them exactly and do not invent identifiers or "
                        "WorkItem details. This rule applies equally when "
                        "assessment_contract_included is false because the resumed "
                        "contract reference carries the same submission values. Use only supplied "
                        "citation claim_ref values must be exactly understanding, "
                        "active_missions:{index}, or decisions_and_constraints:{index}. "
                        "Use only an ID from audited_history_event_ids as a citation "
                        "evidence_ref. "
                        "Before submitting, include at least one citation for "
                        "understanding and for every active_missions and "
                        "decisions_and_constraints item, using its exact indexed "
                        "claim_ref. If any required claim lacks evidence, request "
                        "one history.read action instead of submitting a partial "
                        "assessment. "
                        "history_cursor must be at least minimum_history_cursor; after "
                        "get_current_state, use history_result.event_cursor for both "
                        "history_cursor and current_state_cursor."
                    ),
                    "assessment_contract_included": request.assessment_contract_included,
                    "reorientation_only": True,
                }
            )
        return prompt_payload

    def _write_request_document(
        self, payload: dict[str, object]
    ) -> tuple[Path, str, int]:
        encoded = self._encode_content_addressed_document(payload)
        encoded_bytes = len(encoded)
        if encoded_bytes > self.max_request_document_bytes:
            raise ProviderInvocationError(
                "RequestDocumentTooLarge",
                "Claude request document exceeds the configured byte limit "
                f"({encoded_bytes}>{self.max_request_document_bytes})",
            )
        path, digest = self._write_content_addressed_bytes(
            self.request_document_directory,
            encoded,
        )
        return path, digest, encoded_bytes

    @classmethod
    def _write_content_addressed_document(
        cls,
        directory: Path,
        payload: dict[str, object],
    ) -> tuple[Path, str]:
        return cls._write_content_addressed_bytes(
            directory,
            cls._encode_content_addressed_document(payload),
        )

    @staticmethod
    def _encode_content_addressed_document(
        payload: dict[str, object],
    ) -> bytes:
        return (_canonical_json(payload) + "\n").encode("utf-8")

    @staticmethod
    def _write_content_addressed_bytes(
        directory: Path,
        encoded: bytes,
    ) -> tuple[Path, str]:
        digest = hashlib.sha256(encoded).hexdigest()
        destination = directory / f"{digest}.json"
        if destination.exists():
            if destination.read_bytes() != encoded:
                raise RuntimeError(
                    "content-addressed Claude document mismatch"
                )
            return destination, digest

        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=directory,
                prefix=f".{digest}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if destination.read_bytes() != encoded:
            raise RuntimeError("Claude document failed post-write verification")
        return destination, digest

    def validate_request(
        self, request: InterfaceTurnRequest | ReorientationTurnRequest
    ) -> None:
        if request.candidate != self.candidate:
            raise ContractError("requested Claude candidate differs from PilotHost")
        if request.permission_mode != self.permission_mode:
            raise ContractError("requested permission mode differs from PilotHost")
        if request.max_budget_usd > self.max_budget_usd:
            raise ContractError("requested Claude budget exceeds the host maximum")
        if request.timeout_seconds > self.timeout_seconds:
            raise ContractError("requested Claude timeout exceeds the host maximum")

    def _parse(
        self,
        completed: subprocess.CompletedProcess[str],
        endpoint: str,
        provider_io_sha256: str,
    ) -> tuple[dict[str, object], str, dict[str, object], str]:
        try:
            outer = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Claude Code returned invalid JSON; provider I/O document "
                f"sha256={provider_io_sha256}",
            ) from exc
        if not isinstance(outer, dict):
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Claude Code returned a non-object response",
            )
        usage, actual_model = self._usage(outer)
        if (
            self.permission_mode == "sandboxed_bypass"
            and usage["permission_rejections"] != 0
        ):
            raise ProviderInvocationError(
                "ClassifierUnexpected",
                "sandboxed_bypass reported a permission rejection",
                usage=usage,
                actual_model=actual_model,
            )
        if completed.returncode != 0:
            raise ProviderInvocationError(
                "ProviderExecutionFailed",
                "Claude Code exited without a successful structured response",
                usage=usage,
                actual_model=actual_model,
            )
        output_model: type[StructuredInterfaceOutput | StructuredReorientationOutput]
        if endpoint == "/v1/reorientation-turn":
            output_model = StructuredReorientationOutput
        else:
            output_model = StructuredInterfaceOutput
        try:
            structured = output_model.model_validate(outer["structured_output"])
        except (KeyError, ValidationError) as exc:
            contract_name = (
                "reorientation"
                if endpoint == "/v1/reorientation-turn"
                else "InterfaceAction"
            )
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Claude Code structured output violated "
                f"{contract_name} schema",
                usage=usage,
                actual_model=actual_model,
            ) from exc
        session_id = outer.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Claude Code did not return a provider session ID",
                usage=usage,
                actual_model=actual_model,
            )
        return (
            structured.model_dump(mode="json"),
            actual_model,
            usage,
            session_id,
        )

    def _usage(self, outer: dict[str, object]) -> tuple[dict[str, object], str]:
        model_usage = outer.get("modelUsage")
        if not isinstance(model_usage, dict) or len(model_usage) != 1:
            raise ProviderInvocationError(
                "ProviderUsageMissing",
                "Claude Code did not report exactly one actual model",
            )
        actual_model = next(iter(model_usage))
        actual = model_usage[actual_model]
        if not isinstance(actual, dict):
            raise ProviderInvocationError(
                "ProviderUsageMissing",
                "Claude Code model usage entry is malformed",
                actual_model=actual_model,
            )
        mappings = {
            "input_tokens": "inputTokens",
            "cache_creation_input_tokens": "cacheCreationInputTokens",
            "cache_read_input_tokens": "cacheReadInputTokens",
            "output_tokens": "outputTokens",
            "cost_usd": "costUSD",
        }
        usage: dict[str, object] = {}
        for target, source in mappings.items():
            value = actual.get(source)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value < 0
            ):
                raise ProviderInvocationError(
                    "ProviderUsageMissing",
                    f"Claude Code did not report valid {source}",
                    actual_model=actual_model,
                )
            usage[target] = value
        duration = outer.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
            raise ProviderInvocationError(
                "ProviderUsageMissing",
                "Claude Code did not report valid duration_ms",
                actual_model=actual_model,
            )
        permission_denials = outer.get("permission_denials")
        if not isinstance(permission_denials, list):
            raise ProviderInvocationError(
                "ProviderUsageMissing",
                "Claude Code did not report permission_denials",
                actual_model=actual_model,
            )
        usage.update(
            {
                "duration_ms": duration,
                "classifier_triggered": (
                    self.permission_mode == "managed_permissions"
                    and bool(permission_denials)
                ),
                "permission_rejections": len(permission_denials),
                "permission_mode": self.permission_mode,
                "model_substitution": False,
            }
        )
        return usage, actual_model


class CodexAdapter:
    def __init__(self, raw: object) -> None:
        data = _exact_fields(
            raw,
            {
                "candidate",
                "executable",
                "cli_version",
                "working_directory_allowlist",
                "sandbox",
                "mcp",
                "max_input_tokens",
                "max_output_tokens",
                "max_total_tokens",
                "timeout_seconds",
            },
            "Codex adapter",
            optional_fields={"win32_codex_sandbox_bypass_enabled"},
        )
        self.candidate = ModelCandidate.model_validate(data["candidate"])
        if (
            self.candidate.adapter != "codex-cli"
            or self.candidate.provider != "openai"
        ):
            raise RuntimeError("coding S1 candidate must use codex-cli/openai")
        self.executable = _nonblank(data["executable"], "Codex executable")
        self.cli_version = _nonblank(data["cli_version"], "Codex CLI version")
        directories = data["working_directory_allowlist"]
        if not isinstance(directories, list) or not directories:
            raise RuntimeError("Codex working directory allowlist must be non-empty")
        self.working_directories = {
            str(Path(_nonblank(value, "Codex working directory")).resolve())
            for value in directories
        }
        if len(self.working_directories) != len(directories) or not all(
            Path(value).is_dir() for value in self.working_directories
        ):
            raise RuntimeError("Codex working directory allowlist is invalid")
        self.sandbox = _nonblank(data["sandbox"], "Codex sandbox")
        if self.sandbox not in {"read-only", "workspace-write"}:
            raise RuntimeError("production Codex sandbox is unsupported")
        bypass_enabled = data.get("win32_codex_sandbox_bypass_enabled", False)
        if not isinstance(bypass_enabled, bool):
            raise RuntimeError("win32_codex_sandbox_bypass_enabled must be boolean")
        self.win32_codex_sandbox_bypass_enabled = bypass_enabled
        self.environment_variables: dict[str, str] = {}
        self.mcp = McpAllowlist(data["mcp"])
        self.mcp.validate_candidate_tools(self.candidate)
        self.max_input_tokens = _positive_integer(
            data["max_input_tokens"], "Codex max input tokens"
        )
        self.max_output_tokens = _positive_integer(
            data["max_output_tokens"], "Codex max output tokens"
        )
        self.max_total_tokens = _positive_integer(
            data["max_total_tokens"], "Codex max total tokens"
        )
        if self.max_total_tokens < self.max_input_tokens + self.max_output_tokens:
            raise RuntimeError("Codex max total tokens must cover input and output")
        self.timeout_seconds = _positive_number(
            data["timeout_seconds"], "Codex timeout"
        )
        # Codex exec --json does not emit the resolved model/effort in its stdout
        # event stream (confirmed on codex-cli 0.144.5). The authoritative record
        # of the actual model and reasoning effort is the per-session rollout file
        # that codex writes under CODEX_HOME/sessions. Resolve the same home codex
        # itself resolves so the actual-model gate reads codex's own record rather
        # than inferring the actual model from the requested flags.
        self.codex_home = Path(
            os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
        ).resolve()

    def bind_environment_instance(self, instance: EnvironmentInstance) -> None:
        """Replace host-local defaults with the commissioned instance binding."""

        workspace = instance.logical_path_bindings.get("workspace-root")
        if workspace is None:
            raise RuntimeError(
                "EnvironmentInstance must bind the workspace-root logical path"
            )
        workspace_path = Path(workspace).resolve()
        if not workspace_path.is_dir():
            raise RuntimeError("EnvironmentInstance workspace-root is not a directory")
        executable = Path(instance.cli_executable_path)
        if not executable.is_absolute() or not executable.is_file():
            raise RuntimeError(
                "EnvironmentInstance CLI executable path must be an existing absolute file"
            )
        self.executable = str(executable)
        self.working_directories = {str(workspace_path)}
        self.codex_home = Path(instance.codex_home).resolve()
        self.environment_variables = dict(instance.environment_variables)

    def _sandbox_arguments(self, sandbox_mode: str) -> list[str]:
        if (
            sys.platform == "win32"
            and self.win32_codex_sandbox_bypass_enabled
        ):
            return ["--dangerously-bypass-approvals-and-sandbox"]
        return ["--sandbox", sandbox_mode]

    def _subprocess_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(self.environment_variables)
        return environment

    def run_preflight(self, verification_tuple: VerificationTuple) -> PreflightObservation:
        """Run exactly one Codex trial and read its authoritative rollout policy."""

        resolved_cwd = sorted(self.working_directories)[0]
        prompt = (
            "Nanihold execution-environment preflight. Do not modify the workspace. "
            "Return a short acknowledgement only."
        )
        argv = [
            self.executable,
            "exec",
            "--json",
            "--cd",
            resolved_cwd,
            *self._sandbox_arguments(verification_tuple.sandbox_mode),
            "--strict-config",
            "--ignore-user-config",
            *self.mcp.codex_config_arguments(),
            prompt,
        ]
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            shell=False,
            cwd=resolved_cwd,
            env=self._subprocess_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise PreflightError("Codex preflight trial exited unsuccessfully")
        thread_ids: list[str] = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PreflightError("Codex preflight returned invalid JSONL") from exc
            if not isinstance(event, dict):
                raise PreflightError("Codex preflight returned a non-object event")
            if event.get("type") == "thread.started" and isinstance(
                event.get("thread_id"), str
            ):
                thread_ids.append(event["thread_id"])
        if len(thread_ids) != 1:
            raise PreflightError("Codex preflight did not report one thread")
        sessions_root = self.codex_home / "sessions"
        try:
            matches = sorted(sessions_root.rglob(f"rollout-*-{thread_ids[0]}.jsonl"))
        except OSError as exc:
            raise PreflightError("Codex preflight rollout directory could not be scanned") from exc
        if len(matches) != 1:
            raise PreflightError("Codex preflight rollout was not uniquely found")
        sandbox_policy: str | None = None
        try:
            for line in matches[0].read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PreflightError("Codex preflight rollout contained invalid JSONL") from exc
                candidate = _nested_string(record, "sandbox_policy")
                if candidate is not None:
                    sandbox_policy = candidate
        except OSError as exc:
            raise PreflightError("Codex preflight rollout could not be read") from exc
        if sandbox_policy is None:
            raise PreflightError("Codex preflight rollout did not report sandbox_policy")
        return PreflightObservation(
            sandbox_policy=sandbox_policy,
            capabilities={"workspace_writable": sandbox_policy == "workspace-write"},
            rollout_ref=str(matches[0]),
        )

    def invoke(
        self, request: WorkExecutionRequest
    ) -> tuple[dict[str, object], str, dict[str, object], str]:
        self.validate_request(request)

        resolved_cwd = str(Path(request.cwd).resolve())
        prompt = _canonical_json(
            {
                "contract": (
                    "Execute this WorkItem in the exact cwd and sandbox selected by "
                    "the PilotHost. Return only StructuredWorkOutput. Do not infer "
                    "missing decisions or report completion without acceptance "
                    "evidence. acceptance_results MUST contain exactly one entry for "
                    "each string in the unmet_acceptance array, in the same order and "
                    "with the same count. Each acceptance_results[].criterion MUST be "
                    "a verbatim, character-for-character copy of the corresponding "
                    "unmet_acceptance string. unmet_acceptance is the authoritative "
                    "source text: copy each string exactly as given and never "
                    "rephrase, translate, summarize, reorder, add, drop, or normalize "
                    "any character, including ASCII spaces between Japanese and "
                    "Latin/numeric characters, punctuation, and character width. A "
                    "criterion that differs from its source string by even one "
                    "character will be rejected."
                    " work_item.agent_name is the authoritative "
                    "individual writer identity. For a reply-authoring WorkItem, "
                    "the agent must explicitly author the body and use the existing "
                    "write_supplemental gateway to submit one reply-draft@1 record, "
                    "anchored through derived_from.observations to the exact incoming "
                    "observation, with channel, recipient, body, and drafted_at in "
                    "the payload. Preserve the individual name in created_by and "
                    "lineage, including the WorkItem and execution identifiers. Do "
                    "not create an automatic reply generator, do not submit "
                    "reply-approval@1, do not call send(), and do not treat a draft "
                    "as delivered. Only owner approval via reply-approval@1 may cause "
                    "the existing lethe-channel-bridge to deliver it; that bridge "
                    "creates send-record@1 anchored to the draft. A request without "
                    "the attributed agent name is invalid; fail fast instead of "
                    "inventing one."
                ),
                "execution_id": request.execution_id,
                "work_item": request.work_item.model_dump(
                    mode="json", exclude_none=True
                ),
                "unmet_acceptance": list(request.unmet_acceptance),
                "event_delta": request.event_delta.model_dump(mode="json"),
                "artifact_refs": [
                    item.model_dump(mode="json") for item in request.artifact_refs
                ],
                "token_budget": request.token_budget.model_dump(mode="json"),
            }
        )
        with tempfile.TemporaryDirectory(prefix="nanihold-pilot-") as temp_dir:
            temp_path = Path(temp_dir)
            schema_path = temp_path / "work-output.schema.json"
            output_path = temp_path / "last-message.json"
            schema_path.write_text(_canonical_json(WORK_SCHEMA), encoding="utf-8")
            argv = [
                self.executable,
                "exec",
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--model",
                self.candidate.model_snapshot,
                "-c",
                f"model_reasoning_effort={json.dumps(self.candidate.effort)}",
                "--cd",
                resolved_cwd,
            ]
            argv.extend(self._sandbox_arguments(self.sandbox))
            argv.extend(("--strict-config", "--ignore-user-config"))
            argv.extend(self.mcp.codex_config_arguments())
            argv.append(prompt)
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                shell=False,
                cwd=resolved_cwd,
                env=self._subprocess_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            result_text = (
                output_path.read_text("utf-8") if output_path.is_file() else ""
            )
        return self._parse(completed, result_text, request)

    def validate_request(self, request: WorkExecutionRequest) -> None:
        if request.candidate != self.candidate:
            raise ContractError("requested Codex candidate differs from PilotHost")
        resolved_cwd = str(Path(request.cwd).resolve())
        if resolved_cwd not in self.working_directories:
            raise ContractError("requested Codex cwd is not in the exact allowlist")
        if request.sandbox != self.sandbox:
            raise ContractError("requested Codex sandbox differs from PilotHost")
        if (
            request.token_budget.max_input_tokens > self.max_input_tokens
            or request.token_budget.max_output_tokens > self.max_output_tokens
            or request.token_budget.max_total_tokens > self.max_total_tokens
        ):
            raise ContractError("requested Codex token budget exceeds host limits")
        if request.timeout_seconds > self.timeout_seconds:
            raise ContractError("requested Codex timeout exceeds the host maximum")

    def _parse(
        self,
        completed: subprocess.CompletedProcess[str],
        result_text: str,
        request: WorkExecutionRequest,
    ) -> tuple[dict[str, object], str, dict[str, object], str]:
        events: list[dict[str, object]] = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProviderInvocationError(
                    "ProviderProtocolError",
                    "Codex exec returned invalid JSONL",
                ) from exc
            if not isinstance(event, dict):
                raise ProviderInvocationError(
                    "ProviderProtocolError",
                    "Codex exec returned a non-object event",
                )
            events.append(event)
        thread_events = [
            event for event in events if event.get("type") == "thread.started"
        ]
        completed_events = [
            event for event in events if event.get("type") == "turn.completed"
        ]
        provider_session_id: str | None = None
        if len(thread_events) == 1:
            candidate_session_id = thread_events[0].get("thread_id")
            if isinstance(candidate_session_id, str) and candidate_session_id:
                provider_session_id = candidate_session_id
        if len(thread_events) != 1 or len(completed_events) != 1:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Codex exec did not report one thread and one completed turn",
                provider_session_id=provider_session_id,
            )
        if provider_session_id is None:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Codex exec did not report a thread ID",
            )
        turn = completed_events[0]
        usage = turn.get("usage")
        actual_model, actual_effort = self._verify_actual_model_effort(
            provider_session_id
        )
        parsed_usage = self._validate_usage(
            usage, provider_session_id=provider_session_id
        )
        if (
            actual_model != self.candidate.model_snapshot
            or actual_effort != self.candidate.effort
        ):
            raise ProviderInvocationError(
                "RequestedActualModelMismatch",
                "Codex actual model or reasoning effort differs from the request",
                usage=parsed_usage,
                actual_model=actual_model,
                provider_session_id=provider_session_id,
            )
        if completed.returncode != 0:
            raise ProviderInvocationError(
                "ProviderExecutionFailed",
                "Codex exec exited without a successful structured response",
                usage=parsed_usage,
                actual_model=actual_model,
                provider_session_id=provider_session_id,
            )
        if (
            parsed_usage["input_tokens"] > request.token_budget.max_input_tokens
            or parsed_usage["output_tokens"] > request.token_budget.max_output_tokens
            or parsed_usage["total_tokens"] > request.token_budget.max_total_tokens
        ):
            raise ProviderInvocationError(
                "BudgetExceeded",
                "Codex reported token usage above the authorized budget",
                usage=parsed_usage,
                actual_model=actual_model,
                provider_session_id=provider_session_id,
            )
        try:
            structured = StructuredWorkOutput.model_validate_json(result_text)
        except ValidationError as exc:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Codex final output violated the work output schema",
                usage=parsed_usage,
                actual_model=actual_model,
                provider_session_id=provider_session_id,
            ) from exc
        expected = list(request.unmet_acceptance)
        actual = [item.criterion for item in structured.acceptance_results]
        if actual != expected:
            raise ProviderInvocationError(
                "AcceptanceCoverageMismatch",
                "Codex result did not cover unmet acceptance in request order",
                usage=parsed_usage,
                actual_model=actual_model,
                provider_session_id=provider_session_id,
            )
        if structured.completed and any(
            not result.satisfied for result in structured.acceptance_results
        ):
            raise ProviderInvocationError(
                "FalseComplete",
                "Codex claimed completion with unsatisfied acceptance",
                usage=parsed_usage,
                actual_model=actual_model,
                provider_session_id=provider_session_id,
            )
        return (
            structured.model_dump(mode="json"),
            actual_model,
            parsed_usage,
            provider_session_id,
        )

    def _verify_actual_model_effort(self, thread_id: str) -> tuple[str, str]:
        # codex exec --json reports the thread (session) id but not the resolved
        # model/effort. Read them from codex's own authoritative session rollout
        # `turn_context` record, located by thread id under CODEX_HOME/sessions.
        # This is a direct read of codex's actual-model record, not an inference
        # from the requested flags; any failure to read it fails the gate closed.
        sessions_root = self.codex_home / "sessions"
        try:
            matches = sorted(sessions_root.rglob(f"rollout-*-{thread_id}.jsonl"))
        except OSError as exc:
            raise ProviderInvocationError(
                "ActualModelUnverifiable",
                "Codex session rollout directory could not be scanned",
                provider_session_id=thread_id,
            ) from exc
        if len(matches) != 1:
            raise ProviderInvocationError(
                "ActualModelUnverifiable",
                "Codex session rollout for the thread was not uniquely found",
                provider_session_id=thread_id,
            )
        model: str | None = None
        effort: str | None = None
        try:
            for line in matches[0].read_text("utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ProviderInvocationError(
                        "ActualModelUnverifiable",
                        "Codex session rollout contained invalid JSONL",
                        provider_session_id=thread_id,
                    ) from exc
                if (
                    not isinstance(record, dict)
                    or record.get("type") != "turn_context"
                ):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                candidate_model = payload.get("model")
                candidate_effort = payload.get("effort")
                if isinstance(candidate_model, str) and isinstance(
                    candidate_effort, str
                ):
                    model, effort = candidate_model, candidate_effort
        except OSError as exc:
            raise ProviderInvocationError(
                "ActualModelUnverifiable",
                "Codex session rollout could not be read for model verification",
                provider_session_id=thread_id,
            ) from exc
        if model is None or effort is None:
            raise ProviderInvocationError(
                "ActualModelUnverifiable",
                "Codex session rollout did not report actual model and effort",
                provider_session_id=thread_id,
            )
        return model, effort

    def _validate_usage(
        self, raw: object, *, provider_session_id: str | None = None
    ) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise ProviderInvocationError(
                "ProviderUsageMissing",
                "Codex exec did not report token usage",
                provider_session_id=provider_session_id,
            )
        required = {
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        }
        if set(raw) != required:
            raise ProviderInvocationError(
                "ProviderUsageMissing",
                "Codex exec token usage fields differ from the exact contract",
                provider_session_id=provider_session_id,
            )
        values: dict[str, int] = {}
        for name in required:
            value = raw[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ProviderInvocationError(
                    "ProviderUsageMissing",
                    f"Codex exec did not report valid {name}",
                    provider_session_id=provider_session_id,
                )
            values[name] = value
        return {
            **values,
            "total_tokens": values["input_tokens"] + values["output_tokens"],
            "model_substitution": False,
        }


def _nonblank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} must be a non-blank string")
    return value


def _positive_number(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value <= 0
    ):
        raise RuntimeError(f"{label} must be positive")
    return float(value)


def _positive_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise RuntimeError(f"{label} must be a positive integer")
    return value


def _config_path(config_path: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (config_path.parent / candidate).resolve()


def _load_kernel_preflight(path: Path) -> dict[str, object]:
    """Read only the execution-environment section from the Kernel TOML."""

    try:
        document = tomllib.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError(f"Kernel config could not be read: {path}") from exc
    if not isinstance(document, dict):
        raise RuntimeError("Kernel config must be a TOML object")
    production = document.get("production_pilot_host")
    if not isinstance(production, Mapping):
        raise RuntimeError("Kernel config has no production_pilot_host section")
    kernel = document.get("kernel")
    data_space = kernel.get("data_space") if isinstance(kernel, Mapping) else None
    settings: dict[str, object] = {
        "enabled": production.get("preflight_enabled", False),
    }
    if isinstance(data_space, Mapping) and "data_space_id" in data_space:
        settings["data_space_id"] = data_space["data_space_id"]
    for kernel_name, preflight_name in (
        ("preflight_cli_version_files", "cli_version_files"),
        ("preflight_cache_path", "cache_path"),
        ("preflight_instance_fingerprint", "instance_fingerprint"),
    ):
        if kernel_name in production:
            settings[preflight_name] = production[kernel_name]
    for name in (
        "environment_contract",
        "environment_contract_artifact",
        "environment_instance",
    ):
        if name in document:
            settings[name] = document[name]
    return settings


def _effective_preflight_config(
    *, config_path: Path, raw_config: object
) -> tuple[dict[str, object], Path]:
    fallback = _exact_fields(
        raw_config,
        {"enabled"},
        "preflight config",
        optional_fields={
            "cli_version_files",
            "cache_path",
            "environment_contract",
            "environment_contract_artifact",
            "environment_instance",
            "instance_fingerprint",
            "kernel_config_path",
            "operational_ledger",
            "data_space_id",
        },
    )
    effective = dict(fallback)
    source_path = config_path
    kernel_path_value = fallback.get("kernel_config_path")
    if kernel_path_value is not None:
        kernel_path = _config_path(
            config_path,
            _nonblank(kernel_path_value, "preflight kernel config path"),
        )
        effective.update(_load_kernel_preflight(kernel_path))
        source_path = kernel_path
    return effective, source_path


def _environment_contract_artifact(
    *, config: Mapping[str, object], base_path: Path
) -> object | None:
    raw = config.get("environment_contract_artifact")
    if raw is None:
        return None
    artifact_config = _exact_fields(
        raw,
        {"store_path", "artifact_key", "artifact_version"},
        "environment contract artifact",
    )
    store = LocalEnvironmentContractStore(
        _config_path(
            base_path,
            _nonblank(artifact_config["store_path"], "contract artifact store path"),
        )
    )
    artifact_key = _nonblank(artifact_config["artifact_key"], "contract artifact key")
    artifact_version = _positive_integer(
        artifact_config["artifact_version"], "contract artifact version"
    )
    return store.get(artifact_key=artifact_key, version=artifact_version)


def _environment_instance(
    *, config: Mapping[str, object], contract: EnvironmentContract
) -> EnvironmentInstance | None:
    raw = config.get("environment_instance")
    if raw is None:
        return None
    instance_config = _exact_fields(
        raw,
        {
            "instance_id",
            "logical_path_bindings",
            "cli_executable_path",
            "codex_home",
        },
        "environment instance",
        optional_fields={"environment_variables", "machine_identity"},
    )
    bindings = instance_config["logical_path_bindings"]
    if not isinstance(bindings, Mapping) or not bindings:
        raise RuntimeError("environment instance logical_path_bindings must be an object")
    if any(
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(path, str)
        or not path.strip()
        for name, path in bindings.items()
    ):
        raise RuntimeError("environment instance path bindings must be non-blank strings")
    environment_variables = instance_config.get("environment_variables", {})
    machine_identity = instance_config.get("machine_identity", {})
    if not isinstance(environment_variables, Mapping) or not isinstance(
        machine_identity, Mapping
    ):
        raise RuntimeError("environment instance metadata must be objects")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in [
            *environment_variables.items(),
            *machine_identity.items(),
        ]
    ):
        raise RuntimeError("environment instance metadata must contain strings")
    return EnvironmentInstance.from_contract(
        contract,
        instance_id=_nonblank(instance_config["instance_id"], "environment instance ID"),
        data_space_id=_nonblank(
            config.get("data_space_id"), "environment instance DataSpace ID"
        ),
        logical_path_bindings=dict(bindings),
        cli_executable_path=_nonblank(
            instance_config["cli_executable_path"],
            "environment instance CLI executable path",
        ),
        codex_home=_nonblank(
            instance_config["codex_home"], "environment instance CODEX_HOME"
        ),
        environment_variables=dict(environment_variables),
        machine_identity=dict(machine_identity),
    )


def _available_memory_bytes() -> int:
    if hasattr(os, "sysconf"):
        try:
            return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError):
            pass
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint32),
                ("memory_load", ctypes.c_uint32),
                ("total_physical", ctypes.c_uint64),
                ("available_physical", ctypes.c_uint64),
                ("total_page_file", ctypes.c_uint64),
                ("available_page_file", ctypes.c_uint64),
                ("total_virtual", ctypes.c_uint64),
                ("available_virtual", ctypes.c_uint64),
                ("available_extended", ctypes.c_uint64),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.total_physical)
    raise RuntimeError("available memory could not be measured")


def _instance_preflight_runner(
    *,
    instance: EnvironmentInstance,
    contract: EnvironmentContract,
    adapter_runner: PreflightRunner,
) -> PreflightRunner:
    def run(verification_tuple: VerificationTuple) -> PreflightObservation:
        raw = adapter_runner(verification_tuple)
        if isinstance(raw, PreflightObservation):
            observation = raw
        elif isinstance(raw, Mapping):
            observation = PreflightObservation.from_mapping(raw)
        else:
            raise PreflightError("adapter preflight returned invalid evidence")
        capabilities = dict(observation.capabilities)
        endpoint_reachable: dict[str, bool] = {}
        requirement = contract.adapters.get(verification_tuple.adapter)
        if requirement is None:
            raise PreflightContractError(
                "preflight adapter is not declared by the environment contract"
            )
        for endpoint in requirement.required_endpoints:
            try:
                with socket.create_connection((endpoint, 443), timeout=5):
                    endpoint_reachable[endpoint] = True
            except OSError:
                endpoint_reachable[endpoint] = False
        capabilities["endpoint_reachable"] = endpoint_reachable
        capabilities["memory_bytes"] = _available_memory_bytes()
        capabilities["shell"] = "posix" if os.name == "posix" else "powershell"
        capabilities["path_mappings"] = {
            name: path
            for name, path in instance.logical_path_bindings.items()
            if Path(path).exists()
        }
        workspace = instance.logical_path_bindings.get("workspace-root")
        if workspace is None:
            capabilities["workspace_writable"] = False
        else:
            probe: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=workspace,
                    prefix=".nanihold-preflight-",
                    delete=False,
                ) as stream:
                    probe = Path(stream.name)
                capabilities["workspace_writable"] = True
            except OSError:
                capabilities["workspace_writable"] = False
            finally:
                if probe is not None:
                    try:
                        probe.unlink()
                    except OSError:
                        pass
        return PreflightObservation(
            sandbox_policy=observation.sandbox_policy,
            capabilities=capabilities,
            rollout_ref=observation.rollout_ref,
        )

    return run


def _atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


class ProductionPilotHost:
    def __init__(
        self,
        config_path: Path,
        log_path: Path,
        *,
        preflight_runner: PreflightRunner | None = None,
        declaration_event_hook: Callable[[DeclarationUpdateEvent], object] | None = None,
        evidence_hook: Callable[[PreflightEvidence], object] | None = None,
    ) -> None:
        raw = json.loads(config_path.read_text("utf-8"))
        data = _exact_fields(
            raw,
            {
                "pilot_host_id",
                "device_id",
                "device_certificate_sha256",
                "bearer_token_env",
                "bind_host",
                "bind_port",
                "receipt_store_path",
                "claude",
                "codex",
            },
            "production PilotHost config",
            optional_fields={"preflight"},
        )
        self.identity = DeviceIdentity(
            pilot_host_id=data["pilot_host_id"],
            device_id=data["device_id"],
            certificate_sha256=data["device_certificate_sha256"],
        )
        bearer_env = _nonblank(data["bearer_token_env"], "bearer token env")
        self.bearer_token = _required_env(bearer_env)
        self.bind_host = _nonblank(data["bind_host"], "bind host")
        self.bind_port = _positive_integer(data["bind_port"], "bind port")
        self.receipts = ReceiptStore(
            Path(
                _nonblank(data["receipt_store_path"], "receipt store path")
            ).resolve()
        )
        self.log_path = log_path.resolve()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.claude = ClaudeAdapter(data["claude"])
        self.codex = CodexAdapter(data["codex"])
        self.preflight_evidence: PreflightEvidence | None = None
        self.preflight_update_events: list[DeclarationUpdateEvent] = []
        self._environment_ledger: LetheOperationalLedger | None = None
        self._preflight_status: dict[str, object] = {
            "enabled": False,
            "cli_version_files": None,
            "cache_path": None,
            "instance_fingerprint": None,
            "environment_fingerprint": None,
        }
        preflight_config = data.get("preflight")
        self.preflight: PreflightGate | None = None
        if preflight_config is not None:
            self.preflight = self._build_preflight(
                config_path=config_path,
                document=data,
                raw_config=preflight_config,
                preflight_runner=preflight_runner,
                declaration_event_hook=declaration_event_hook,
                evidence_hook=evidence_hook,
            )

    def _build_preflight(
        self,
        *,
        config_path: Path,
        document: dict[str, Any],
        raw_config: object,
        preflight_runner: PreflightRunner | None,
        declaration_event_hook: Callable[[DeclarationUpdateEvent], object] | None,
        evidence_hook: Callable[[PreflightEvidence], object] | None,
    ) -> PreflightGate | None:
        config, source_path = _effective_preflight_config(
            config_path=config_path,
            raw_config=raw_config,
        )
        if not isinstance(config["enabled"], bool):
            raise RuntimeError("preflight enabled must be boolean")
        if not config["enabled"]:
            self._preflight_status["enabled"] = False
            return None
        raw_version_files = config.get("cli_version_files")
        if not isinstance(raw_version_files, Mapping) or not raw_version_files:
            raise RuntimeError("preflight CLI version files must be an object")
        cli_version_files = {
            adapter: _config_path(
                source_path,
                _nonblank(path, f"preflight CLI version file for {adapter}"),
            )
            for adapter, path in raw_version_files.items()
        }
        cache_path = _config_path(
            source_path,
            _nonblank(config["cache_path"], "preflight cache path"),
        )
        instance_fingerprint = _nonblank(
            config["instance_fingerprint"], "preflight instance fingerprint"
        )
        contract: EnvironmentContract | None = None
        contract_raw = config.get("environment_contract")
        if contract_raw is not None:
            if not isinstance(contract_raw, Mapping):
                raise RuntimeError("preflight environment_contract must be an object")
            try:
                contract = EnvironmentContract.model_validate(contract_raw)
            except ValidationError as exc:
                raise RuntimeError("preflight environment_contract is invalid") from exc
        artifact = _environment_contract_artifact(
            config=config,
            base_path=source_path,
        )
        if artifact is not None:
            artifact_contract = artifact.contract
            if contract is not None and contract != artifact_contract:
                raise RuntimeError(
                    "Kernel environment_contract differs from its local artifact"
                )
            contract = artifact_contract
        if contract is None:
            raise RuntimeError(
                "preflight requires environment_contract or a local contract artifact"
            )
        missing_version_files = sorted(set(contract.adapters) - set(cli_version_files))
        if missing_version_files:
            raise RuntimeError(
                "preflight CLI version files are missing adapters: "
                + ", ".join(missing_version_files)
            )
        contract_fingerprint = environment_fingerprint(contract)
        self._preflight_status = {
            "enabled": True,
            "cli_version_files": {
                adapter: str(path) for adapter, path in cli_version_files.items()
            },
            "cache_path": str(cache_path),
            "instance_fingerprint": instance_fingerprint,
            "environment_fingerprint": contract_fingerprint,
        }
        declarations = {
            "claude-code": document["claude"]["candidate"],
            "codex-cli": document["codex"]["candidate"],
        }
        for adapter, declaration in declarations.items():
            if not isinstance(declaration, dict):
                raise RuntimeError(f"{adapter} candidate declaration must be an object")
            if declaration.get("environment_fingerprint") != contract_fingerprint:
                raise RuntimeError(
                    f"preflight environment fingerprint differs from the {adapter} candidate"
                )

        def record_event(event: DeclarationUpdateEvent) -> None:
            self.preflight_update_events.append(event)
            if declaration_event_hook is not None:
                declaration_event_hook(event)

        def record_evidence(evidence: PreflightEvidence) -> None:
            self.preflight_evidence = evidence
            if effective_evidence_hook is not None:
                effective_evidence_hook(evidence)

        instance = _environment_instance(config=config, contract=contract)
        effective_evidence_hook = evidence_hook
        if instance is not None:
            if instance.instance_fingerprint != instance_fingerprint:
                raise RuntimeError(
                    "preflight instance fingerprint differs from the EnvironmentInstance binding"
                )
            self.codex.bind_environment_instance(instance)
            if effective_evidence_hook is None:
                ledger_raw = config.get("operational_ledger")
                ledger_config = _exact_fields(
                    ledger_raw,
                    {
                        "base_url",
                        "bearer_token_env",
                        "data_space_id",
                        "timeout_seconds",
                        "max_page_size",
                    },
                    "preflight operational ledger",
                )
                ledger_data_space_id = _nonblank(
                    ledger_config["data_space_id"],
                    "preflight operational ledger DataSpace ID",
                )
                if ledger_data_space_id != instance.data_space_id:
                    raise RuntimeError(
                        "preflight operational ledger DataSpace differs from the instance"
                    )
                self._environment_ledger = LetheOperationalLedger(
                    base_url=_nonblank(
                        ledger_config["base_url"],
                        "preflight operational ledger base URL",
                    ),
                    bearer_token=_required_env(
                        _nonblank(
                            ledger_config["bearer_token_env"],
                            "preflight operational ledger bearer token env",
                        )
                    ),
                    data_space_id=ledger_data_space_id,
                    timeout_seconds=_positive_number(
                        ledger_config["timeout_seconds"],
                        "preflight operational ledger timeout",
                    ),
                    max_page_size=_positive_integer(
                        ledger_config["max_page_size"],
                        "preflight operational ledger max page size",
                    ),
                )
                lifecycle = EnvironmentInstanceService(
                    data_space_id=instance.data_space_id,
                    ledger=self._environment_ledger,
                    clock=utc_now,
                )
                lifecycle.attach_active(instance, contract=contract)
                effective_evidence_hook = lifecycle.preflight_evidence_hook(
                    instance.instance_id,
                    idempotency_key_prefix="environment:preflight:verify",
                )
        runners: dict[str, PreflightRunner] = {
            adapter: preflight_runner
            for adapter in contract.adapters
        } if preflight_runner is not None else {
            "claude-code": self.claude.run_preflight,
            "codex-cli": self.codex.run_preflight,
        }
        if instance is not None and preflight_runner is None:
            runners = {
                adapter: _instance_preflight_runner(
                    instance=instance,
                    contract=contract,
                    adapter_runner=runner,
                )
                for adapter, runner in runners.items()
            }
        return PreflightGate(
            contract=contract,
            instance_fingerprint=instance_fingerprint,
            version_readers={
                adapter: CliVersionReader(path)
                for adapter, path in cli_version_files.items()
            },
            cache_path=cache_path,
            preflight_runners=runners,
            candidate_declarations=declarations,
            declaration_event_hook=record_event,
            declaration_persist_hook=lambda: _atomic_write_json(config_path, document),
            evidence_hook=record_evidence,
        )

    def close(self) -> None:
        if self._environment_ledger is not None:
            self._environment_ledger.close()

    def authorized(self, headers: Any) -> bool:
        expected = {
            "Authorization": f"Bearer {self.bearer_token}",
            "X-Nanihold-Pilot-Host-Id": self.identity.pilot_host_id,
            "X-Nanihold-Device-Id": self.identity.device_id,
            "X-Nanihold-Device-Certificate-Sha256": (
                self.identity.certificate_sha256
            ),
        }
        return all(
            hmac.compare_digest(headers.get(name, ""), value)
            for name, value in expected.items()
        )

    def log(self, record: dict[str, object]) -> None:
        allowed = {
            "timestamp",
            "method",
            "path",
            "status",
            "receipt_id",
            "error_code",
        }
        if not set(record).issubset(allowed):
            raise RuntimeError("attempted to log a field outside the safe allowlist")
        with self.log_path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical_json(record) + "\n")

    def health(self) -> dict[str, object]:
        return {
            "status": "ready",
            "identity": self.identity.model_dump(mode="json"),
            "endpoints": [
                "/v1/interface-turn",
                "/v1/reorientation-turn",
                "/v1/work-executions",
                "/v1/receipts/{id}",
            ],
            "candidates": {
                "interface": {
                    "candidate_key": _candidate_key(self.claude.candidate),
                    "selection": self.claude.candidate.selection,
                    "model_snapshot": self.claude.candidate.model_snapshot,
                    "effort": self.claude.candidate.effort,
                },
                "coding_s1": {
                    "candidate_key": _candidate_key(self.codex.candidate),
                    "selection": self.codex.candidate.selection,
                    "model_snapshot": self.codex.candidate.model_snapshot,
                    "effort": self.codex.candidate.effort,
                },
            },
            "permission_mode": self.claude.permission_mode,
            "max_request_document_bytes": (
                self.claude.max_request_document_bytes
            ),
            "receipt_reconciliation": True,
            "preflight": dict(self._preflight_status),
        }

    def execute(self, endpoint: str, payload: dict[str, object]) -> dict[str, object]:
        model_type: type[
            InterfaceTurnRequest | ReorientationTurnRequest | WorkExecutionRequest
        ]
        if endpoint == "/v1/interface-turn":
            model_type = InterfaceTurnRequest
        elif endpoint == "/v1/reorientation-turn":
            model_type = ReorientationTurnRequest
        elif endpoint == "/v1/work-executions":
            model_type = WorkExecutionRequest
        else:
            raise ContractError("undefined PilotHost endpoint")
        try:
            request = model_type.model_validate(payload)
        except ValidationError as exc:
            raise ContractError("request violates the exact endpoint contract") from exc
        if request.device_identity != self.identity:
            raise ContractError("request device identity differs from PilotHost")
        if isinstance(request, WorkExecutionRequest):
            self.codex.validate_request(request)
            if (
                sys.platform == "win32"
                and self.codex.sandbox == "workspace-write"
                and not self.codex.win32_codex_sandbox_bypass_enabled
                and self.preflight is None
            ):
                raise ContractError(
                    "Windows workspace-write execution requires a successful "
                    "EnvironmentContract preflight"
                )
        else:
            self.claude.validate_request(request)
        if self.preflight is not None:
            try:
                self.preflight.dispatch_preflight(request.candidate.adapter)
            except PreflightError as exc:
                raise ContractError(
                    f"preflight rejected the execution: {exc}"
                ) from exc

        request_digest = _request_sha256(request)
        receipt, created = self.receipts.begin(
            endpoint=endpoint,
            receipt_id=request.receipt_id,
            idempotency_key=request.idempotency_key,
            request_sha256=request_digest,
            candidate_key=_candidate_key(request.candidate),
            requested_model=request.candidate.model_snapshot,
        )
        if not created:
            return receipt
        try:
            if isinstance(request, WorkExecutionRequest):
                result, actual_model, usage, session_id = self.codex.invoke(request)
            else:
                result, actual_model, usage, session_id = self.claude.invoke(
                    endpoint, request
                )
        except subprocess.TimeoutExpired:
            return self.receipts.finish(
                request.receipt_id,
                status="failed",
                actual_model=None,
                provider_session_id=None,
                usage=None,
                result=None,
                error={
                    "code": "ProviderTimeout",
                    "message": "provider process exceeded the authorized timeout",
                },
            )
        except ProviderInvocationError as exc:
            return self.receipts.finish(
                request.receipt_id,
                status="failed",
                actual_model=exc.actual_model,
                provider_session_id=exc.provider_session_id,
                usage=exc.usage,
                result=None,
                error={"code": exc.code, "message": exc.safe_message},
            )
        except OSError:
            return self.receipts.finish(
                request.receipt_id,
                status="failed",
                actual_model=None,
                provider_session_id=None,
                usage=None,
                result=None,
                error={
                    "code": "ProviderLaunchFailed",
                    "message": "provider process could not be launched",
                },
            )
        except Exception:
            return self.receipts.finish(
                request.receipt_id,
                status="failed",
                actual_model=None,
                provider_session_id=None,
                usage=None,
                result=None,
                error={
                    "code": "PilotHostInternalError",
                    "message": "PilotHost could not validate the provider result",
                },
            )
        return self.receipts.finish(
            request.receipt_id,
            status="succeeded",
            actual_model=actual_model,
            provider_session_id=session_id,
            usage=usage,
            result=result,
            error=None,
        )


def _handler(host: ProductionPilotHost):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NaniholdProductionPilotHost/1"

        def _send(self, status: HTTPStatus, body: dict[str, object]) -> None:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _authenticate(self) -> bool:
            if host.authorized(self.headers):
                return True
            self._send(
                HTTPStatus.UNAUTHORIZED,
                {"error": "exact PilotHost device authentication required"},
            )
            return False

        def do_GET(self) -> None:
            if not self._authenticate():
                return
            if self.path == "/health":
                self._send(HTTPStatus.OK, host.health())
                return
            prefix = "/v1/receipts/"
            if self.path.startswith(prefix) and len(self.path) > len(prefix):
                receipt_id = self.path[len(prefix) :]
                if "/" in receipt_id or "?" in receipt_id:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                receipt = host.receipts.get(receipt_id)
                if receipt is None:
                    self._send(HTTPStatus.NOT_FOUND, {"error": "receipt not found"})
                    return
                self._send(HTTPStatus.OK, receipt)
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authenticate():
                return
            if self.path not in {
                "/v1/interface-turn",
                "/v1/reorientation-turn",
                "/v1/work-executions",
            }:
                self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise ContractError("Content-Length is required")
                length = int(raw_length)
                if length <= 0 or length > MAX_REQUEST_BYTES:
                    raise ContractError("invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ContractError("request must be an object")
                receipt = host.execute(self.path, payload)
            except (ContractError, json.JSONDecodeError, ValueError) as exc:
                self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                return
            except ConflictError as exc:
                self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            self._send(HTTPStatus.OK, receipt)

        def log_message(self, format: str, *args: object) -> None:
            # BaseHTTPRequestHandler's default log is intentionally suppressed.
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    args = parser.parse_args()
    host = ProductionPilotHost(args.config.resolve(), args.log_file.resolve())
    try:
        host.log(
            {
                "timestamp": _utc_now(),
                "method": "START",
                "path": "/",
                "status": "ready",
            }
        )
        server = ThreadingHTTPServer(
            (host.bind_host, host.bind_port),
            _handler(host),
        )
        server.serve_forever()
        return 0
    finally:
        host.close()


if __name__ == "__main__":
    raise SystemExit(main())
