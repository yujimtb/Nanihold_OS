"""vsm CLI: Run 投入・指示・状態確認・replay の操作入口。

The ``vsm`` console script defined in ``pyproject.toml`` resolves to the
:data:`app` Typer application below. Four subcommands are exposed; each
maps directly onto a section of the spec:

==========  ==================================================================
Subcommand  Validates Requirements
==========  ==================================================================
submit      4.1〜4.7, 12.7〜12.9, 14.1〜14.8
status      10.2, 11.1, 11.7
tail        10.2, 11.2, 11.3, 11.4, 11.7
replay      10.2, 11.5, 11.6, 11.7
==========  ==================================================================

Heavy modules (``asyncio``, the runtime, the LLM provider, the System
classes) are imported lazily inside command bodies so that ``vsm --help``
and CLI argument validation remain fast for users running the binary
without a full Run-time stack.

REQ 14.8 (out-of-MVP-scope rejection) is enforced by a custom Click
group class :class:`_ScopeGuardGroup` that overrides
:meth:`resolve_command` to intercept out-of-scope subcommand names at
the parser layer (before Click's "no such command" handler fires). The
top-level :func:`_scope_guard` callback adds a defence-in-depth second
layer that inspects ``ctx.invoked_subcommand`` and ``sys.argv``. Either
layer alone is sufficient; together they cover both in-process
(:class:`typer.testing.CliRunner`) and direct CLI (``python -m vsm``)
invocations. The seven scope-violating capability names enumerated by
REQ 14.1〜14.7 are intentionally not registered as subcommands either,
so the dispatcher itself has no handler for them and a forbidden
capability cannot be invoked through normal CLI use.

Exit code summary (design.md §Error Handling §Exit Code 体系):

==== ============================================================
code Meaning
==== ============================================================
0    Normal completion
2    CLI input validation failure (REQ 4.2, 4.5, 10.2, 11.7)
3    Structural constraint violation (REQ 13.2, surfaced by ConfigError)
4    Run directory creation failure (REQ 10.4)
5    Out-of-MVP-scope capability requested (REQ 14.8)
6    Representative scenario 1800 s timeout (REQ 12.9)
==== ============================================================
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import click
import typer
from typer.core import TyperGroup

from vsm.clock import SystemClock
from vsm.errors import CLIError, ConfigError, RunDirectoryError
from vsm.ids import generate_run_id, generate_uuid, validate_run_id

__all__ = ["app"]


# REQ 14.1〜14.7: capability names that are explicitly out of MVP scope.
# Comparison is case-insensitive (see :class:`_ScopeGuardGroup` and
# :func:`_scope_guard`) so that ``FSx`` / ``Fsx`` / ``fsx`` are all caught
# uniformly.
OUT_OF_SCOPE_NAMES: frozenset[str] = frozenset(
    {
        "fsx",
        "publicness",
        "shared-surplus",
        "human-intervention",
        "recursive-growth",
        "semi-stateful-mix",
        "web-ui",
    }
)


# ---------------------------------------------------------------------------
# Scope guard at the Click resolution layer (REQ 14.8)
# ---------------------------------------------------------------------------


class _ScopeGuardGroup(TyperGroup):
    """Reject out-of-MVP-scope subcommand names at command-resolution time.

    Click dispatches subcommand resolution via :meth:`Group.resolve_command`
    *before* any registered callback fires; if the requested name is not a
    registered subcommand, Click raises a ``UsageError`` (exit code 2) and
    the callback never runs. That breaks the REQ 14.8 contract for any
    invocation path that does not mutate ``sys.argv`` -- notably
    :class:`typer.testing.CliRunner`, which threads args through Click's
    context directly.

    Overriding :meth:`resolve_command` here intercepts the requested
    subcommand name on the *args* the parser is actually working with and
    rejects scope-out names with a typed :class:`typer.Exit` (code 5)
    plus the canonical ``requested capability is out of MVP scope: <name>``
    stderr message. The match is case-insensitive so ``FSX`` / ``Fsx`` /
    ``fsX`` are all rejected uniformly.

    The seven scope-violating names enumerated by REQ 14.1〜14.7 remain
    intentionally absent from the registered subcommand set, so this guard
    is a *necessary* layer (no command exists to dispatch to) rather than
    an opportunistic short-circuit.
    """

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[Optional[str], Optional[click.Command], list[str]]:
        if args:
            requested = args[0]
            if (
                isinstance(requested, str)
                and requested.lower() in OUT_OF_SCOPE_NAMES
            ):
                typer.echo(
                    f"requested capability is out of MVP scope: {requested}",
                    err=True,
                )
                raise typer.Exit(code=5)
        return super().resolve_command(ctx, args)


# ---------------------------------------------------------------------------
# Typer application
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="vsm",
    help="Viable System Model PoC Platform CLI.",
    no_args_is_help=True,
    add_completion=False,
    cls=_ScopeGuardGroup,
)
selfdev_app = typer.Typer(
    name="selfdev",
    help="自己開発 Proposal の REST 操作。",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(selfdev_app, name="selfdev")

_SELFDEV_API_BASE = "http://127.0.0.1:8000/api/selfdev"

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

# REQ 4.2: description bounds (1..8192 Unicode characters).
_DESCRIPTION_MIN: int = 1
_DESCRIPTION_MAX: int = 8192

# REQ 4.5: file size upper bound (1 MiB / 1,048,576 bytes).
_FILE_MAX_BYTES: int = 1_048_576

# REQ 12.9: representative scenario completion deadline.
_RUN_TIMEOUT_SECONDS: float = 1800.0

# Polling interval used by :func:`submit` while waiting for completion
# events to arrive on disk. One second leaves a wide margin against the
# 30-minute scenario budget while keeping idle CPU usage negligible.
_COMPLETION_POLL_INTERVAL_SECONDS: float = 1.0

# Keep long-running submits from looking idle while the runtime is still
# producing events slowly.
_PROGRESS_HEARTBEAT_SECONDS: float = 15.0

# Roles whose presence in the Event_Log is required for a Run to be
# considered complete (REQ 12.7, 12.8). Mirrors the canonical role
# strings emitted by ``system_instantiated`` payloads (see
# :class:`vsm.roles.SystemRole`).
_REQUIRED_COMPLETION_ROLES: frozenset[str] = frozenset(
    {
        "S1_WORKER",
        "S2_COORDINATOR",
        "S3_ALLOCATOR",
        "S3STAR_AUDITOR",
        "S4_SCANNER",
        "S5_POLICY",
    }
)


# ---------------------------------------------------------------------------
# Scope guard (REQ 14.8)
# ---------------------------------------------------------------------------


@app.callback()
def _scope_guard(ctx: typer.Context) -> None:
    """Defence-in-depth REQ 14.8 guard, complementing :class:`_ScopeGuardGroup`.

    The primary scope-out rejection happens in
    :meth:`_ScopeGuardGroup.resolve_command`, which fires *before* this
    callback during normal Click dispatch (so the canonical rejection
    path never reaches here). This callback remains as a redundant
    second layer that:

    * rejects scope-out names that already won the resolver race (e.g.
      a hypothetical future Typer/Click change that lets the callback
      fire ahead of resolution), and
    * inspects ``ctx.invoked_subcommand`` and raw ``sys.argv`` so that
      both in-process (:class:`typer.testing.CliRunner`) and direct CLI
      (``python -m vsm fsx``) invocations are covered even if the
      resolver layer is bypassed.

    Comparison is case-insensitive against :data:`OUT_OF_SCOPE_NAMES`.
    """
    candidates: list[str] = []
    if ctx.invoked_subcommand:
        candidates.append(ctx.invoked_subcommand)
    candidates.extend(sys.argv[1:])
    for token in candidates:
        if isinstance(token, str) and token.lower() in OUT_OF_SCOPE_NAMES:
            typer.echo(
                f"requested capability is out of MVP scope: {token}",
                err=True,
            )
            raise typer.Exit(code=5)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validate_description(description: str) -> None:
    """REQ 4.2: enforce ``1 <= len(description) <= 8192``.

    On violation, writes the canonical message to stderr and exits with
    code 2. Length is measured in Python Unicode characters, so Japanese
    input is accepted without special handling.
    """
    length = len(description)
    if length < _DESCRIPTION_MIN:
        _emit_cli_error(
            "Task description cannot be empty.",
            example='vsm submit "Summarize this incident"',
            next_step="Pass a task description between 1 and 8192 characters.",
        )
        raise typer.Exit(code=2)
    if length > _DESCRIPTION_MAX:
        _emit_cli_error(
            f"Task description length is {length}; the maximum is "
            f"{_DESCRIPTION_MAX} characters.",
            example='vsm submit "Summarize this incident"',
            next_step="Shorten the description or put long context in --file.",
        )
        raise typer.Exit(code=2)


def _read_context_file(path: Path) -> str:
    """REQ 4.5: validate ``path`` and return its UTF-8 decoded contents.

    A file is rejected (exit code 2) if it does not exist, exceeds 1 MiB,
    or is not valid UTF-8. The reason string in the stderr message is
    stable so callers can pattern-match on it.
    """
    if not path.exists():
        typer.echo(f"file {path}: does not exist", err=True)
        raise typer.Exit(code=2)
    try:
        size = path.stat().st_size
    except OSError as exc:
        # ``stat`` after a successful ``exists`` should not normally fail
        # in production, but we handle it defensively to keep CLI errors
        # uniform.
        typer.echo(f"file {path}: cannot stat ({exc})", err=True)
        raise typer.Exit(code=2) from None
    if size > _FILE_MAX_BYTES:
        typer.echo(
            f"file {path}: exceeds {_FILE_MAX_BYTES} bytes (REQ 4.5)",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        typer.echo(f"file {path}: not valid UTF-8", err=True)
        raise typer.Exit(code=2) from None
    except OSError as exc:
        typer.echo(f"file {path}: read failed ({exc})", err=True)
        raise typer.Exit(code=2) from None


def _validate_run_id_or_exit(run_id: str) -> None:
    """REQ 10.2: format-validate ``run_id`` or exit with the embedded code.

    :func:`vsm.ids.validate_run_id` raises :class:`CLIError` with
    ``exit_code=2`` on format violations; this helper translates that
    typed error into the corresponding ``typer.Exit`` so each CLI
    subcommand can short-circuit with a uniform message.
    """
    try:
        validate_run_id(run_id)
    except CLIError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.exit_code) from None


def _events_path_for(run_id: str) -> Path:
    """Resolve ``runs/{run_id}/events.jsonl`` (REQ 10.3)."""
    return Path("runs") / run_id / "events.jsonl"


def _require_events_path(run_id: str) -> Path:
    """REQ 11.7: return the events path or exit 2 with the canonical message."""
    path = _events_path_for(run_id)
    if not path.exists():
        _emit_cli_error(
            f"No events found for run {run_id}.",
            next_step="Check available Runs with: vsm runs",
        )
        raise typer.Exit(code=2)
    return path


def _emit_cli_error(
    message: str,
    *,
    example: str | None = None,
    next_step: str | None = None,
) -> None:
    typer.echo(f"Error: {message}", err=True)
    if example is not None:
        typer.echo(f"Example: {example}", err=True)
    if next_step is not None:
        typer.echo(f"Next: {next_step}", err=True)


def _clean_error_text(exc: BaseException) -> str:
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    return text.replace("REQ ", "requirement ")


def _short_id(value: object, width: int = 12) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    return text[:width]


def _short_text(value: object, width: int = 96) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    if not text:
        return "-"
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _format_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    return value


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _read_events(path: Path) -> list[dict[str, Any]]:
    from vsm.eventlog.reader import read_all

    return read_all(path)


def _role_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
    role_order = {
        "S5_POLICY": 0,
        "S4_SCANNER": 1,
        "S3_ALLOCATOR": 2,
        "S2_COORDINATOR": 3,
        "S3STAR_AUDITOR": 4,
        "S1_WORKER": 5,
    }
    system_id, info = item
    role = str(info.get("role", ""))
    return role_order.get(role, 99), system_id


def _derive_task_state(
    replay_state: object,
    events: list[dict[str, Any]],
) -> tuple[str, str]:
    state = str(replay_state or "unknown")
    source = "replay"
    for evt in events:
        if evt.get("event_type") == "task_state_changed":
            payload = evt.get("payload", {}) or {}
            to_state = payload.get("to_state")
            if isinstance(to_state, str):
                state = to_state
                source = "task_state_changed"
    if any(evt.get("event_type") == "s1_completion" for evt in events):
        return "completed", "s1_completion"
    error_events = [
        evt
        for evt in events
        if str(evt.get("event_type", "")).endswith("_error")
        or evt.get("event_type") in {"llm_timeout", "tool_failed", "node_failed"}
    ]
    if error_events and state in {"submitted", "running", "unknown"}:
        return "failed", str(error_events[-1].get("event_type"))
    return state, source


def _derive_run_state(events: list[dict[str, Any]]) -> str:
    if not events:
        return "empty"
    event_types = {str(evt.get("event_type", "")) for evt in events}
    if "s1_completion" in event_types or "web_run_completed" in event_types:
        return "completed"
    if event_types & {"web_run_cancelled", "node_terminated"}:
        return "stopped"
    if (
        any(event_type.endswith("_error") for event_type in event_types)
        or "llm_timeout" in event_types
        or "tool_failed" in event_types
        or "node_failed" in event_types
    ):
        return "failed"
    if "task_submitted" in event_types:
        return "submitted"
    if "web_run_created" in event_types:
        return "created"
    return "events"


@dataclass(frozen=True)
class _RunSummary:
    run_id: str
    short_run_id: str
    started_at: str
    state: str
    active: bool
    event_count: int
    task_description: str
    tokens_consumed: int
    wall_clock_ms: int
    sort_mtime: float


def _budget_totals(events: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for evt in events:
        if evt.get("event_type") != "budget_consumed":
            continue
        payload = evt.get("payload", {}) or {}
        node_id = str(payload.get("node_id") or evt.get("node_id") or "unknown")
        item = totals.setdefault(
            node_id,
            {"tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0, "wall_clock_ms": 0},
        )
        for key in item:
            value = payload.get(key, 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                item[key] += int(value)
    return totals


def _summarise_run_dir(run_dir: Path) -> _RunSummary:
    events_path = run_dir / "events.jsonl"
    events = _read_events(events_path)
    first_event = events[0] if events else {}
    task_description = "-"
    for evt in events:
        if evt.get("event_type") == "task_submitted":
            payload = evt.get("payload", {}) or {}
            task_description = _short_text(payload.get("description"), 80)
            break
    budgets = _budget_totals(events)
    return _RunSummary(
        run_id=run_dir.name,
        short_run_id=_short_id(run_dir.name),
        started_at=_format_timestamp(first_event.get("ts")),
        state=_derive_run_state(events),
        active=(run_dir / "RUNNING").exists(),
        event_count=len(events),
        task_description=task_description,
        tokens_consumed=sum(
            item["tokens_in"] + item["tokens_out"] + item["tokens_cache_read"]
            for item in budgets.values()
        ),
        wall_clock_ms=sum(item["wall_clock_ms"] for item in budgets.values()),
        sort_mtime=events_path.stat().st_mtime,
    )


def _emit_progress(
    *,
    clock: SystemClock,
    started: float,
    phase: str,
    message: str,
) -> None:
    elapsed = int(clock.monotonic() - started)
    typer.echo(f"[{elapsed:4d}s] {phase:<17} {message}", err=True)


def _progress_for_event(evt: dict[str, Any]) -> tuple[str, str] | None:
    event_type = evt.get("event_type")
    payload = evt.get("payload", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    if event_type == "task_submitted":
        return "S4_SCANNER", "Task accepted"
    if event_type == "system_instantiated":
        role = payload.get("role")
        if isinstance(role, str):
            return role, "System ready"
    if event_type == "s4_assessment_produced":
        return "S4_SCANNER", "Assessment produced"
    if event_type == "policy_decision":
        return "S5_POLICY", "Policy decision produced"
    if event_type == "s1_instantiated":
        return "S1_WORKER", "Worker spawned"
    if event_type == "s1_assignment_sent":
        return "S3_ALLOCATOR", "Assignment sent"
    if event_type == "s1_completion":
        return "S1_WORKER", "Completion received"
    if isinstance(event_type, str) and (
        event_type.endswith("_error")
        or event_type in {"llm_timeout", "tool_failed", "node_failed"}
    ):
        return "ERROR", event_type
    return None


def _replay_line(evt: dict[str, Any]) -> str:
    ts = evt.get("ts", "-")
    payload = evt.get("payload", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    sys_id = payload.get("system_id") or payload.get("sender") or "-"
    channel = payload.get("channel") or "-"
    event_type = evt.get("event_type", "-")
    return f"{ts} {sys_id} {channel} {event_type}"


def _event_summary(evt: dict[str, Any]) -> str | None:
    event_type = evt.get("event_type")
    payload = evt.get("payload", {}) or {}
    if not isinstance(payload, dict):
        return None

    if event_type == "task_submitted":
        desc = payload.get("description")
        file_paths = payload.get("file_paths") or []
        return f"task: {_short_text(desc, 96)} (files={len(file_paths)})"

    if event_type == "system_instantiated":
        role = payload.get("role")
        count = payload.get("sub_agent_count")
        return f"system: {role} Sub_Agents={count}"

    if event_type == "llm_invocation":
        model = payload.get("model")
        latency = payload.get("latency_ms")
        response = payload.get("response")
        return f"llm: {model} {latency} ms response={_short_text(response, 72)}"

    if event_type == "llm_timeout":
        return f"error: LLM call timed out after {payload.get('elapsed_ms')} ms"

    if event_type == "llm_error":
        code = payload.get("provider_code")
        message = payload.get("provider_message")
        detail = f"{code}: {message}" if code else message
        return f"error: LLM provider {_short_text(detail, 96)}"

    if event_type == "sub_agent_error":
        reason = payload.get("reason")
        if isinstance(reason, str):
            return f"error: sub-agent {_short_text(reason, 96)}"

    if event_type in {
        "system_instantiation_failed",
        "delivery_error",
        "dispatch_error",
        "s1_instantiation_error",
        "event_log_append_error",
        "tool_failed",
        "node_failed",
    }:
        reason = payload.get("reason") or payload.get("error")
        if isinstance(reason, str):
            return f"error: {_short_text(reason, 96)}"

    if event_type == "channel_rejected":
        return (
            "error: channel rejected "
            f"{payload.get('sender')} -> {payload.get('receiver')} "
            f"on {payload.get('channel')}"
        )

    if event_type == "s1_completion":
        result = payload.get("result")
        if isinstance(result, dict):
            success = result.get("success")
            text = result.get("text")
            if isinstance(text, str):
                return f"result: success={success} {_short_text(text, 96)}"
            return "result: " + _short_text(
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                96,
            )

    if event_type == "policy_decision":
        directive = payload.get("directive")
        if isinstance(directive, str):
            return f"decision: {_short_text(directive, 96)}"

    if event_type == "s4_assessment_produced":
        opportunities = payload.get("opportunities") or []
        threats = payload.get("threats") or []
        return f"assessment: opportunities={len(opportunities)} threats={len(threats)}"

    if event_type == "audit_finding":
        content = payload.get("content")
        if isinstance(content, str):
            return f"finding: {_short_text(content, 96)}"

    if event_type == "tool_invoked":
        tool_name = payload.get("tool_name") or payload.get("name")
        return f"tool: {_short_text(tool_name, 48)} invoked"

    if event_type == "tool_completed":
        tool_name = payload.get("tool_name") or payload.get("name")
        result = payload.get("result")
        if isinstance(result, dict):
            result_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        else:
            result_text = result
        return f"tool: {_short_text(tool_name, 48)} completed {_short_text(result_text, 72)}"

    if event_type in {"node_created", "node_started", "node_idled"}:
        node_id = payload.get("node_id") or evt.get("node_id")
        return f"node: {_short_text(node_id, 48)}"

    if event_type and str(event_type).startswith("web_"):
        state = payload.get("state") or payload.get("to_state")
        if state is not None:
            return f"web: state={state}"

    return None


# ---------------------------------------------------------------------------
# selfdev REST client
# ---------------------------------------------------------------------------


def _selfdev_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """自己開発 CLI の唯一の transport 経路。

    API 停止時に Event Log を直接開く経路は持たない。接続失敗と HTTP
    エラーはそのまま CLI の失敗として扱う。
    """

    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    url = f"{_SELFDEV_API_BASE}{path}"
    request = Request(
        url,
        data=(json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None),
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        typer.echo(f"selfdev API が拒否しました ({exc.code}): {detail}", err=True)
        raise typer.Exit(code=1) from None
    except URLError as exc:
        typer.echo(
            f"Nanihold selfdev API に接続できません ({_SELFDEV_API_BASE}): {exc.reason}",
            err=True,
        )
        raise typer.Exit(code=1) from None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        typer.echo(f"selfdev API の応答が JSON ではありません: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not isinstance(value, dict):
        typer.echo("selfdev API の応答は JSON object でなければなりません", err=True)
        raise typer.Exit(code=1)
    return value


def _emit_selfdev_json(value: dict[str, Any], *, compact: bool = True) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=None if compact else 2))


def _require_proposal_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        typer.echo(f"proposal file {path}: does not exist", err=True)
        raise typer.Exit(code=2) from None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        typer.echo(f"proposal file {path}: read failed ({exc})", err=True)
        raise typer.Exit(code=2) from None
    if not isinstance(value, dict):
        typer.echo("proposal file は JSON object でなければなりません", err=True)
        raise typer.Exit(code=2)
    allowed = {
        "title",
        "motivation",
        "scope",
        "acceptance_criteria",
        "risk_class",
        "budget_estimate",
        "origin",
        "dependencies",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        typer.echo(f"proposal file に controller 管理フィールドがあります: {unknown}", err=True)
        raise typer.Exit(code=2)
    return value


@selfdev_app.command("propose")
def selfdev_propose(
    file: Path = typer.Option(..., "--file", exists=False, help="Proposal 作成 request の JSON file."),
) -> None:
    """ProposalManifest の controller 管理フィールドを除いた request を投入する。"""

    _emit_selfdev_json(_selfdev_request("POST", "/proposals", _require_proposal_file(file)))


@selfdev_app.command("list")
def selfdev_list(
    state: str | None = typer.Option(None, "--state", help="ProposalPhase."),
    pending_action: str | None = typer.Option(None, "--pending-action", help="human のみ."),
    json_output: bool = typer.Option(False, "--json", help="canonical JSON を出力する."),
) -> None:
    """Proposal を状態または Human 承認待ちで一覧する。"""

    from urllib.parse import urlencode

    query: dict[str, str] = {}
    if state:
        query["state"] = state
    if pending_action:
        query["pending_action"] = pending_action
    suffix = f"?{urlencode(query)}" if query else ""
    value = _selfdev_request("GET", f"/proposals{suffix}")
    if json_output:
        _emit_selfdev_json(value)
        return
    for item in value.get("items", []):
        typer.echo(
            f"{item.get('proposal_id')}  {item.get('state')}  "
            f"{item.get('title')}  updated={item.get('updated_at')}"
        )


@selfdev_app.command("show")
def selfdev_show(
    proposal_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="canonical JSON を出力する."),
) -> None:
    """Proposal の専用 projection を表示する。"""

    from urllib.parse import quote

    value = _selfdev_request("GET", f"/proposals/{quote(proposal_id, safe='')}")
    _emit_selfdev_json(value, compact=json_output)


def _selfdev_control_command(
    proposal_id: str,
    action: str,
    reason: str,
    state_version: int,
) -> None:
    from urllib.parse import quote

    value = _selfdev_request(
        "POST",
        f"/proposals/{quote(proposal_id, safe='')}/control",
        {"action": action, "reason": reason, "expected_state_version": state_version},
    )
    _emit_selfdev_json(value)


@selfdev_app.command("suspend")
def selfdev_suspend(proposal_id: str, reason: str = typer.Option(..., "--reason"), state_version: int = typer.Option(..., "--state-version")) -> None:
    _selfdev_control_command(proposal_id, "suspend", reason, state_version)


@selfdev_app.command("resume")
def selfdev_resume(proposal_id: str, reason: str = typer.Option(..., "--reason"), state_version: int = typer.Option(..., "--state-version")) -> None:
    _selfdev_control_command(proposal_id, "resume", reason, state_version)


@selfdev_app.command("abort")
def selfdev_abort(proposal_id: str, reason: str = typer.Option(..., "--reason"), state_version: int = typer.Option(..., "--state-version")) -> None:
    _selfdev_control_command(proposal_id, "abort", reason, state_version)


def _selfdev_human_command(
    proposal_id: str,
    decision: str,
    reason: str,
    state_version: int,
    statement: str | None = None,
) -> None:
    from urllib.parse import quote

    payload: dict[str, Any] = {
        "decision": decision,
        "reason": reason,
        "statement": statement,
        "expected_state_version": state_version,
    }
    if decision == "approve":
        detail = _selfdev_request("GET", f"/proposals/{quote(proposal_id, safe='')}")
        for field in ("proposal_manifest_sha256", "protected_scope_sha256"):
            value = detail.get(field)
            if not isinstance(value, str) or not value:
                typer.echo(f"Proposal detail に {field} がありません", err=True)
                raise typer.Exit(code=1)
            payload[field] = value
    value = _selfdev_request(
        "POST", f"/proposals/{quote(proposal_id, safe='')}/human-decision", payload
    )
    _emit_selfdev_json(value)


@selfdev_app.command("approve")
def selfdev_approve(proposal_id: str, reason: str = typer.Option(..., "--reason"), state_version: int = typer.Option(..., "--state-version")) -> None:
    _selfdev_human_command(proposal_id, "approve", reason, state_version)


@selfdev_app.command("reject")
def selfdev_reject(proposal_id: str, reason: str = typer.Option(..., "--reason"), state_version: int = typer.Option(..., "--state-version")) -> None:
    _selfdev_human_command(proposal_id, "reject", reason, state_version)


@selfdev_app.command("respond")
def selfdev_respond(proposal_id: str, statement: str = typer.Option(..., "--statement"), state_version: int = typer.Option(..., "--state-version")) -> None:
    _selfdev_human_command(proposal_id, "respond", "", state_version, statement=statement)


@selfdev_app.command("outcome")
def selfdev_outcome(
    proposal_id: str,
    merged: bool = typer.Option(False, "--merged"),
    archived: bool = typer.Option(False, "--archived"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    if merged == archived:
        typer.echo("--merged または --archived のどちらか一方が必要です", err=True)
        raise typer.Exit(code=2)
    from urllib.parse import quote

    value = _selfdev_request(
        "POST",
        f"/proposals/{quote(proposal_id, safe='')}/merge-outcome",
        {"merged": merged, "reason": reason},
    )
    _emit_selfdev_json(value)


# ---------------------------------------------------------------------------
# submit (Task 19.1)
# ---------------------------------------------------------------------------


@app.command()
def instruct(
    run_id: str = typer.Argument(..., help="Run identifier."),
    text: str = typer.Argument(..., help="Human instruction text."),
    node: Optional[str] = typer.Option(None, "--node", help="Target Node id. Defaults to S5."),
) -> None:
    """実行中 Run の Node へローカル REST API 経由で追加指示を送る。"""

    _validate_run_id_or_exit(run_id)
    if not text.strip():
        typer.echo("instruction must not be empty", err=True)
        raise typer.Exit(code=2)

    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    payload: dict[str, Any] = {"instruction": text.strip()}
    if node is not None:
        payload["target_node"] = node
    request = Request(
        f"http://127.0.0.1:8000/api/runs/{run_id}/instructions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        typer.echo(f"instruction API rejected the request ({exc.code}): {detail}", err=True)
        raise typer.Exit(code=1) from None
    except URLError as exc:
        typer.echo(
            f"Nanihold API に接続できません (http://127.0.0.1:8000): {exc.reason}",
            err=True,
        )
        raise typer.Exit(code=1) from None
    typer.echo(json.dumps(result, ensure_ascii=False))


@app.command()
def submit(
    description: str = typer.Argument(
        ...,
        help="Task description, 1..8192 characters.",
    ),
    file: Optional[list[Path]] = typer.Option(
        None,
        "--file",
        "-f",
        help="Optional UTF-8 context file. Repeat the option for multiple files.",
    ),
) -> None:
    """Submit a task and wait for the VSM Run to finish.

    Progress is printed to stderr while the Run is active. On success,
    stdout contains only run_id=... and task_id=... so scripts can parse
    the identifiers safely.

    Examples:
      vsm submit "Write a short architecture summary"
      vsm submit "Review this design note" --file notes.txt
    """
    # ---- input validation -------------------------------------------------
    _validate_description(description)
    file_paths: list[Path] = list(file or [])
    # REQ 4.5: validate every supplied file before any side effects (Run
    # directory creation, Event_Log open, etc.). The contents themselves
    # are not currently propagated into the task payload, but the read
    # itself confirms UTF-8 validity per the REQ.
    for fp in file_paths:
        _read_context_file(fp)

    # ---- identifier and timestamp generation (REQ 4.6) -------------------
    run_id = generate_run_id()
    task_id = generate_uuid()
    clock = SystemClock()
    submitted_at = clock.now_iso()
    task_payload: dict[str, Any] = {
        "task_id": task_id,
        "run_id": run_id,
        "description": description,
        "file_paths": [str(p) for p in file_paths],
        "submitted_at": submitted_at,
    }

    # ---- async run body ---------------------------------------------------
    # Heavy imports (asyncio + runtime + reader) are deferred to here so
    # ``vsm --help`` does not pay their startup cost.
    import asyncio

    from vsm.config import load_config
    from vsm.eventlog.reader import read_all
    from vsm.roles import SystemRole
    from vsm.runtime.lifecycle import start_run

    async def _run() -> None:
        llm_config, run_config = load_config(None)
        platform = await start_run(
            run_id=run_id,
            run_config=run_config,
            llm_config=llm_config,
            lethe_context_query=description,
        )
        events_path = platform.run_dir / "events.jsonl"
        progress_started = clock.monotonic()
        last_progress_at = progress_started
        last_seq_seen = -1
        current_phase = "S4_SCANNER"
        try:
            _emit_progress(
                clock=clock,
                started=progress_started,
                phase=current_phase,
                message="Run started",
            )
            # REQ 4.6: persist the Task acceptance on the Event_Log.
            await platform.eventlog.append("task_submitted", task_payload)

            # Trigger S4_Scanner with the Task. ``MANDATORY_ROLES``
            # guarantees at least one S4 instance exists by the time
            # :func:`start_run` returns.
            s4_instances = platform.systems.get(SystemRole.S4_SCANNER, [])
            if not s4_instances:
                # Defensive: should never happen because lifecycle
                # validation rejects missing roles before we get here.
                raise RuntimeError(
                    "S4_SCANNER instance not present after start_run"
                )
            s4 = s4_instances[0]
            trigger = getattr(s4, "trigger", None)
            if trigger is None:
                raise RuntimeError(
                    "S4_SCANNER instance does not expose .trigger()"
                )
            await trigger(task_payload)

            # Wait for completion: poll the Event_Log every second until
            # we observe an ``s1_completion`` event AND each of the six
            # mandatory roles has been instantiated (REQ 12.7, 12.8).
            deadline = clock.monotonic() + _RUN_TIMEOUT_SECONDS
            while clock.monotonic() < deadline:
                await asyncio.sleep(_COMPLETION_POLL_INTERVAL_SECONDS)
                if not events_path.exists():
                    # ``EventLogWriter`` creates the file on the first
                    # write; if it has not appeared yet, keep waiting.
                    continue
                events = read_all(events_path)
                event_types: set[str] = set()
                roles_seen: set[str] = set()
                for evt in events:
                    event_types.add(evt["event_type"])
                    if evt["event_type"] == "system_instantiated":
                        role = evt.get("payload", {}).get("role")
                        if isinstance(role, str):
                            roles_seen.add(role)
                    seq = evt.get("seq")
                    if isinstance(seq, int) and seq > last_seq_seen:
                        progress = _progress_for_event(evt)
                        if progress is not None:
                            current_phase, message = progress
                            _emit_progress(
                                clock=clock,
                                started=progress_started,
                                phase=current_phase,
                                message=message,
                            )
                            last_progress_at = clock.monotonic()
                        last_seq_seen = max(last_seq_seen, seq)

                now = clock.monotonic()
                if now - last_progress_at >= _PROGRESS_HEARTBEAT_SECONDS:
                    _emit_progress(
                        clock=clock,
                        started=progress_started,
                        phase=current_phase,
                        message="Still running",
                    )
                    last_progress_at = now
                if (
                    "s1_completion" in event_types
                    and _REQUIRED_COMPLETION_ROLES.issubset(roles_seen)
                ):
                    return

            # REQ 12.9: 1800 second deadline exceeded.
            _emit_cli_error(
                f"Run {run_id} did not finish within "
                f"{_RUN_TIMEOUT_SECONDS:.0f} seconds.",
                next_step=f"Inspect the partial run with: vsm replay {run_id}",
            )
            raise typer.Exit(code=6)
        finally:
            await platform.shutdown()

    try:
        asyncio.run(_run())
    except ConfigError as exc:
        # REQ 13.2: structural constraint violation (e.g. mandatory
        # System missing). The lifecycle layer has already written
        # ``missing required systems: ...`` to stderr; we just translate
        # the typed error into exit code 3.
        detail = _clean_error_text(exc)
        if "LLM provider" in detail or "LITELLM_PROVIDER" in detail:
            _emit_cli_error(
                "No LLM provider is configured.",
                example='$env:LITELLM_PROVIDER = "openai/gpt-4o-mini"',
                next_step=(
                    "Set LITELLM_PROVIDER in the shell, .env, or vsm.toml "
                    "before running vsm submit."
                ),
            )
        else:
            _emit_cli_error(
                f"Configuration is invalid: {detail}",
                next_step="Fix the configuration, then run vsm submit again.",
            )
        raise typer.Exit(code=3) from None
    except RunDirectoryError as exc:
        # REQ 10.4: ``runs/{run_id}/`` could not be created.
        _emit_cli_error(
            f"Could not create the run directory. {_clean_error_text(exc)}",
            next_step="Check write access to runs/ and remove any conflicting run directory.",
        )
        raise typer.Exit(code=4) from None
    except typer.Exit:
        # ``_run`` raises ``typer.Exit(code=6)`` on the timeout path;
        # propagate it without rewrapping.
        raise

    # REQ 4.7: emit the identifiers on stdout. Two lines makes the
    # output trivially parseable from shell scripts.
    typer.echo(f"run_id={run_id}")
    typer.echo(f"task_id={task_id}")


# ---------------------------------------------------------------------------
# status (Task 20.1)
# ---------------------------------------------------------------------------


@app.command()
def status(
    run_id: str = typer.Argument(..., help="Run identifier."),
) -> None:
    """Show a readable summary for one Run.

    The summary is rebuilt from events.jsonl. Task state is derived from
    existing events such as s1_completion, so completed Runs no longer
    appear stuck at submitted.

    Example:
      vsm status run-1234567890abcdef1234567890abcdef
    """
    _validate_run_id_or_exit(run_id)
    path = _require_events_path(run_id)

    # Heavy import deferred until after argument validation.
    from vsm.eventlog.replay import replay

    events = _read_events(path)
    state = replay(path)

    typer.echo(f"Run: {run_id}")
    typer.echo(f"Events: {len(events)}")
    typer.echo("")
    typer.echo("Tasks:")
    if not state.tasks:
        typer.echo("  none")
    for task_id, info in state.tasks.items():
        derived_state, source = _derive_task_state(info.get("state"), events)
        description = _short_text(info.get("description", ""), 96)
        submitted_at = _format_timestamp(info.get("submitted_at"))
        file_count = len(info.get("file_paths", []) or [])
        typer.echo(
            f"  - task {_short_id(task_id)}  state: {derived_state} "
            f"(from {source})"
        )
        typer.echo(f"    description: {description}")
        typer.echo(f"    submitted: {submitted_at}  files: {file_count}")

    typer.echo("")
    typer.echo("Systems:")
    if not state.systems:
        typer.echo("  none")
    for system_id, info in sorted(state.systems.items(), key=_role_sort_key):
        role = info.get("role", "UNKNOWN")
        sub_agent_count = info.get("sub_agent_count", "?")
        typer.echo(
            f"  - {role:<17} id: {_short_id(system_id)}  "
            f"Sub_Agents: {sub_agent_count}"
        )

    typer.echo("")
    typer.echo("Budget consumption by Node:")
    budgets = _budget_totals(events)
    if not budgets:
        typer.echo("  none")
    roles_by_node = {
        str((evt.get("payload", {}) or {}).get("system_id")): (evt.get("payload", {}) or {}).get("role", "UNKNOWN")
        for evt in events
        if evt.get("event_type") == "system_instantiated"
    }
    for node_id, consumed in sorted(budgets.items()):
        total = consumed["tokens_in"] + consumed["tokens_out"] + consumed["tokens_cache_read"]
        typer.echo(
            f"  - {roles_by_node.get(node_id, 'UNKNOWN'):<17} id: {_short_id(node_id)}  "
            f"tokens: {total} (in {consumed['tokens_in']} / out {consumed['tokens_out']} / "
            f"cache {consumed['tokens_cache_read']})  wall: {consumed['wall_clock_ms'] / 1000:.3f}s"
        )


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


@app.command("runs")
def list_runs(
    limit: int = typer.Option(
        20,
        "--limit",
        "-n",
        min=1,
        help="Maximum number of Runs to show.",
    ),
    full_id: bool = typer.Option(
        False,
        "--full-id",
        help="Show full Run ids instead of shortened ids.",
    ),
    runs_dir: Path = typer.Option(
        Path("runs"),
        "--runs-dir",
        help="Directory that contains Run folders.",
    ),
) -> None:
    """List recent Runs in a readable format.

    Runs are sorted newest first. Each row shows a shortened Run id, start
    time, derived state, event count, and the first Task description.

    Examples:
      vsm runs
      vsm runs --limit 5
    """
    if not runs_dir.exists():
        typer.echo(f"No Runs found under {runs_dir}.")
        return
    if not runs_dir.is_dir():
        _emit_cli_error(
            f"Runs path is not a directory: {runs_dir}",
            example="vsm runs --runs-dir runs",
            next_step="Pass a directory that contains run-* folders.",
        )
        raise typer.Exit(code=2)

    all_dirs = [path for path in runs_dir.iterdir() if path.is_dir()]
    run_dirs = [path for path in all_dirs if (path / "events.jsonl").is_file()]
    skipped_count = len(all_dirs) - len(run_dirs)
    if not run_dirs:
        typer.echo(f"No Runs found under {runs_dir}.")
        if skipped_count:
            typer.echo(
                f"Note: ignored {skipped_count} "
                f"{_plural(skipped_count, 'directory', 'directories')} "
                "without events.jsonl."
            )
        return

    summaries = [_summarise_run_dir(path) for path in run_dirs]
    summaries.sort(key=lambda item: item.sort_mtime, reverse=True)
    summaries = summaries[:limit]

    rows = []
    for summary in summaries:
        state = f"{summary.state}+active" if summary.active else summary.state
        display_id = summary.run_id if full_id else summary.short_run_id
        rows.append(
            (
                display_id,
                summary.started_at,
                state,
                str(summary.event_count),
                str(summary.tokens_consumed),
                f"{summary.wall_clock_ms / 1000:.3f}s",
                summary.task_description,
            )
        )

    id_width = max(len("RUN ID"), *(len(row[0]) for row in rows))
    started_width = max(len("STARTED"), *(len(row[1]) for row in rows))
    state_width = max(len("STATE"), *(len(row[2]) for row in rows))
    events_width = max(len("EVENTS"), *(len(row[3]) for row in rows))
    tokens_width = max(len("TOKENS"), *(len(row[4]) for row in rows))
    wall_width = max(len("WALL"), *(len(row[5]) for row in rows))
    typer.echo("Run list (newest first)")
    typer.echo(
        f"{'RUN ID':<{id_width}}  "
        f"{'STARTED':<{started_width}}  "
        f"{'STATE':<{state_width}}  "
        f"{'EVENTS':>{events_width}}  "
        f"{'TOKENS':>{tokens_width}}  "
        f"{'WALL':>{wall_width}}  "
        "TASK"
    )
    for display_id, started_at, state, event_count, tokens, wall, task_description in rows:
        typer.echo(
            f"{display_id:<{id_width}}  "
            f"{started_at:<{started_width}}  "
            f"{state:<{state_width}}  "
            f"{event_count:>{events_width}}  "
            f"{tokens:>{tokens_width}}  "
            f"{wall:>{wall_width}}  "
            f"{task_description}"
        )
    if skipped_count:
        typer.echo(
            f"Note: ignored {skipped_count} "
            f"{_plural(skipped_count, 'directory', 'directories')} "
            "without events.jsonl."
        )


# ---------------------------------------------------------------------------
# tail (Task 21.1)
# ---------------------------------------------------------------------------


def _build_tail_predicate(
    system_filters: list[str],
    channel_filters: list[str],
) -> Callable[[dict[str, Any]], bool]:
    """Compose ``--system`` and ``--channel`` filters into a single predicate.

    REQ 11.3: multiple ``--system`` values combine with OR; multiple
    ``--channel`` values combine with OR; the two groups combine with
    AND. REQ 11.4: when both filter lists are empty the predicate
    accepts every event.

    The system match inspects three common payload keys
    (``system_id`` / ``sender`` / ``receiver``) so that a single
    ``--system`` flag matches both Channel events (which use
    sender/receiver) and lifecycle events (which use system_id) without
    forcing the user to know the schema.
    """

    def predicate(evt: dict[str, Any]) -> bool:
        payload = evt.get("payload", {}) or {}
        if system_filters:
            sys_id = payload.get("system_id")
            sender = payload.get("sender")
            receiver = payload.get("receiver")
            if not any(
                sf == sys_id or sf == sender or sf == receiver
                for sf in system_filters
            ):
                return False
        if channel_filters:
            channel = payload.get("channel")
            if channel not in channel_filters:
                return False
        return True

    return predicate


@app.command()
def tail(
    run_id: str = typer.Argument(..., help="Run identifier."),
    system: Optional[list[str]] = typer.Option(
        None,
        "--system",
        "-s",
        help="Show events involving this system id. Repeat to match any.",
    ),
    channel: Optional[list[str]] = typer.Option(
        None,
        "--channel",
        "-c",
        help="Show events on this channel. Repeat to match any.",
    ),
) -> None:
    """Follow new events for a Run.

    Output stays as JSONL so it can still be piped to other tools. Use
    filters to focus on one System or one VSM channel. Press Ctrl-C to stop.

    Examples:
      vsm tail run-1234567890abcdef1234567890abcdef
      vsm tail run-1234567890abcdef1234567890abcdef --channel S4-S5
    """
    _validate_run_id_or_exit(run_id)
    path = _require_events_path(run_id)

    system_filters: list[str] = list(system or [])
    channel_filters: list[str] = list(channel or [])
    predicate = _build_tail_predicate(system_filters, channel_filters)

    # Heavy imports deferred until after argument validation.
    import asyncio

    from vsm.eventlog.reader import iter_appended

    async def _tail() -> None:
        try:
            async for evt in iter_appended(path, predicate):
                # ``ensure_ascii=False`` keeps non-ASCII payloads (e.g.
                # 営業 / リサーチ Sub_Agent labels from REQ 5.1) readable
                # in the terminal output. ``default=str`` is a defensive
                # fallback for any non-serialisable values that sneak in.
                typer.echo(
                    json.dumps(evt, ensure_ascii=False, default=str)
                )
        except CLIError as exc:
            # ``iter_appended`` raises CLIError(exit_code=2) if the path
            # disappears mid-tail. We already verified existence above
            # but the generator is defensive in case of a race.
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=exc.exit_code) from None

    try:
        asyncio.run(_tail())
    except KeyboardInterrupt:
        # REQ 11.2: Ctrl-C is the documented termination path. Exit
        # cleanly so shells do not surface a Python traceback.
        return


# ---------------------------------------------------------------------------
# replay (Task 22.1)
# ---------------------------------------------------------------------------


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="Run identifier."),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print the legacy one-line event format without payload summaries.",
    ),
) -> None:
    """Print a Run's events in append order.

    Each line starts with timestamp, system id, channel, and event type.
    Missing fields are shown as a dash. By default, selected payload fields
    are summarised on the following indented line. Use --raw for the legacy
    one-line format.

    Example:
      vsm replay run-1234567890abcdef1234567890abcdef
    """
    _validate_run_id_or_exit(run_id)
    path = _require_events_path(run_id)

    # REQ 11.6: warn (on stderr, before any stdout output) if the Run is
    # still active. The lockfile is created by
    # :class:`vsm.runtime.lifecycle.Platform` at Run start and removed
    # by :meth:`Platform.shutdown`; observing it post-shutdown therefore
    # is a strong indicator the Run is still in flight.
    lockfile = path.parent / "RUNNING"
    if lockfile.exists():
        typer.echo(f"warning: run {run_id} is still active", err=True)

    # Heavy import deferred until after argument validation.
    from vsm.eventlog.reader import read_all

    events = read_all(path)
    for evt in events:
        typer.echo(_replay_line(evt))
        if not raw:
            summary = _event_summary(evt)
            if summary is not None:
                typer.echo(f"  {summary}")


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    app()
