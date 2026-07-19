from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from vsm.interface.models import InterfaceAction
from vsm.pilot.models import DeviceIdentity, ModelCandidate


CLAUDE_MODEL = "claude-fable-5"
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


class ReorientationTurnRequest(ClaudeTurnRequest):
    objective: Annotated[str, Field(min_length=1, max_length=20_000)]
    session_index_ref: Annotated[str, Field(min_length=1)]
    open_commitment_refs: tuple[str, ...]
    current_state_ref: Annotated[str, Field(min_length=1)]


class ArtifactRef(StrictModel):
    artifact_id: Annotated[str, Field(min_length=1)]
    sha256: Annotated[str, Field(pattern=SHA256_PATTERN)]
    media_type: Annotated[str, Field(min_length=1)]


class WorkItemHandoff(StrictModel):
    work_item_id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1, max_length=2_000)]
    objective: Annotated[str, Field(min_length=1, max_length=100_000)]


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
    display_text: Annotated[str, Field(min_length=1)]
    actions: tuple[InterfaceAction, ...]


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


INTERFACE_SCHEMA = StructuredInterfaceOutput.model_json_schema()
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
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.usage = usage
        self.actual_model = actual_model


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


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def _exact_fields(value: object, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError(f"{label} fields differ from the exact contract")
    return value


def _candidate_key(candidate: ModelCandidate) -> str:
    return candidate.key


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
        requested_model: str,
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
            or self.candidate.model_snapshot != CLAUDE_MODEL
            or self.candidate.effort != CLAUDE_EFFORT
        ):
            raise RuntimeError(
                "Claude Interface candidate must be "
                "claude-code/anthropic/claude-fable-5/high"
            )
        self.executable = _nonblank(data["executable"], "Claude executable")
        self.cli_version = _nonblank(data["cli_version"], "Claude CLI version")
        if self.candidate.adapter_version != self.cli_version:
            raise RuntimeError("Claude candidate adapter version mismatch")
        self.working_directory = Path(
            _nonblank(data["working_directory"], "Claude working directory")
        ).resolve()
        if not self.working_directory.is_dir():
            raise RuntimeError("Claude working directory does not exist")
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
        self._validate_version()

    def _validate_version(self) -> None:
        completed = subprocess.run(
            [self.executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if completed.returncode != 0 or self.cli_version not in completed.stdout:
            raise RuntimeError("Claude CLI version mismatch")

    def invoke(
        self,
        endpoint: str,
        request: InterfaceTurnRequest | ReorientationTurnRequest,
    ) -> tuple[dict[str, object], str, dict[str, object], str]:
        self.validate_request(request)

        prompt_payload: dict[str, object] = {
            "contract": (
                "Return one StructuredInterfaceOutput. You are the Interface Pilot, "
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
                    "reorientation_only": True,
                }
            )

        permission_flag = {
            "sandboxed_bypass": "bypassPermissions",
            "managed_permissions": "auto",
            "observe_only": "plan",
        }[self.permission_mode]
        argv = [
            self.executable,
            "-p",
            _canonical_json(prompt_payload),
            "--output-format",
            "json",
            "--json-schema",
            _canonical_json(INTERFACE_SCHEMA),
            "--model",
            self.candidate.model_snapshot,
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
            timeout=request.timeout_seconds,
            shell=False,
            cwd=self.working_directory,
        )
        return self._parse(completed, endpoint)

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
        self, completed: subprocess.CompletedProcess[str], endpoint: str
    ) -> tuple[dict[str, object], str, dict[str, object], str]:
        try:
            outer = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Claude Code returned invalid JSON",
            ) from exc
        if not isinstance(outer, dict):
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Claude Code returned a non-object response",
            )
        usage, actual_model = self._usage(outer)
        if actual_model != self.candidate.model_snapshot:
            raise ProviderInvocationError(
                "RequestedActualModelMismatch",
                "Claude Code actual model differs from the requested snapshot",
                usage=usage,
                actual_model=actual_model,
            )
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
        try:
            structured = StructuredInterfaceOutput.model_validate(
                outer["structured_output"]
            )
        except (KeyError, ValidationError) as exc:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Claude Code structured output violated InterfaceAction schema",
                usage=usage,
                actual_model=actual_model,
            ) from exc
        if endpoint == "/v1/reorientation-turn":
            allowed = {"history.read", "reorientation.submit"}
            if any(action.kind not in allowed for action in structured.actions):
                raise ProviderInvocationError(
                    "ReorientationEffectForbidden",
                    "reorientation turn returned an action outside read/submit scope",
                    usage=usage,
                    actual_model=actual_model,
                )
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
                "model_substitution": (
                    actual_model != self.candidate.model_snapshot
                ),
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
        )
        self.candidate = ModelCandidate.model_validate(data["candidate"])
        if (
            self.candidate.adapter != "codex-cli"
            or self.candidate.provider != "openai"
        ):
            raise RuntimeError("coding S1 candidate must use codex-cli/openai")
        self.executable = _nonblank(data["executable"], "Codex executable")
        self.cli_version = _nonblank(data["cli_version"], "Codex CLI version")
        if self.candidate.adapter_version != self.cli_version:
            raise RuntimeError("Codex candidate adapter version mismatch")
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
        self._validate_version()

    def _validate_version(self) -> None:
        completed = subprocess.run(
            [self.executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            shell=False,
        )
        if completed.returncode != 0 or self.cli_version not in completed.stdout:
            raise RuntimeError("Codex CLI version mismatch")

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
                    "missing decisions or report completion without acceptance evidence."
                ),
                "execution_id": request.execution_id,
                "work_item": request.work_item.model_dump(mode="json"),
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
                "--sandbox",
                self.sandbox,
                "--strict-config",
                "--ignore-user-config",
            ]
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
        if len(thread_events) != 1 or len(completed_events) != 1:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Codex exec did not report one thread and one completed turn",
            )
        provider_session_id = thread_events[0].get("thread_id")
        if not isinstance(provider_session_id, str) or not provider_session_id:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Codex exec did not report a thread ID",
            )
        turn = completed_events[0]
        actual_model = turn.get("model")
        actual_effort = turn.get("model_reasoning_effort")
        usage = turn.get("usage")
        if not isinstance(actual_model, str) or not isinstance(actual_effort, str):
            raise ProviderInvocationError(
                "ActualModelUnverifiable",
                "Codex exec JSONL did not report actual model and reasoning effort",
            )
        parsed_usage = self._validate_usage(usage)
        if (
            actual_model != self.candidate.model_snapshot
            or actual_effort != self.candidate.effort
        ):
            raise ProviderInvocationError(
                "RequestedActualModelMismatch",
                "Codex actual model or reasoning effort differs from the request",
                usage=parsed_usage,
                actual_model=actual_model,
            )
        if completed.returncode != 0:
            raise ProviderInvocationError(
                "ProviderExecutionFailed",
                "Codex exec exited without a successful structured response",
                usage=parsed_usage,
                actual_model=actual_model,
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
            )
        try:
            structured = StructuredWorkOutput.model_validate_json(result_text)
        except ValidationError as exc:
            raise ProviderInvocationError(
                "ProviderProtocolError",
                "Codex final output violated the work output schema",
                usage=parsed_usage,
                actual_model=actual_model,
            ) from exc
        expected = list(request.unmet_acceptance)
        actual = [item.criterion for item in structured.acceptance_results]
        if actual != expected:
            raise ProviderInvocationError(
                "AcceptanceCoverageMismatch",
                "Codex result did not cover unmet acceptance in request order",
                usage=parsed_usage,
                actual_model=actual_model,
            )
        if structured.completed and any(
            not result.satisfied for result in structured.acceptance_results
        ):
            raise ProviderInvocationError(
                "FalseComplete",
                "Codex claimed completion with unsatisfied acceptance",
                usage=parsed_usage,
                actual_model=actual_model,
            )
        return (
            structured.model_dump(mode="json"),
            actual_model,
            parsed_usage,
            provider_session_id,
        )

    def _validate_usage(self, raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise ProviderInvocationError(
                "ProviderUsageMissing",
                "Codex exec did not report token usage",
            )
        required = {"input_tokens", "cached_input_tokens", "output_tokens"}
        if set(raw) != required:
            raise ProviderInvocationError(
                "ProviderUsageMissing",
                "Codex exec token usage fields differ from the exact contract",
            )
        values: dict[str, int] = {}
        for name in required:
            value = raw[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ProviderInvocationError(
                    "ProviderUsageMissing",
                    f"Codex exec did not report valid {name}",
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


class ProductionPilotHost:
    def __init__(self, config_path: Path, log_path: Path) -> None:
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
                    "model_snapshot": self.claude.candidate.model_snapshot,
                    "effort": self.claude.candidate.effort,
                },
                "coding_s1": {
                    "candidate_key": _candidate_key(self.codex.candidate),
                    "model_snapshot": self.codex.candidate.model_snapshot,
                    "effort": self.codex.candidate.effort,
                },
            },
            "permission_mode": self.claude.permission_mode,
            "receipt_reconciliation": True,
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
        else:
            self.claude.validate_request(request)

        request_digest = _sha256_json(request.model_dump(mode="json"))
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
                provider_session_id=None,
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


if __name__ == "__main__":
    raise SystemExit(main())
