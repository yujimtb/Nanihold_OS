"""vsm CLI: ``submit`` / ``status`` / ``tail`` / ``replay`` subcommands.

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
import re
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
    help=(
        "VSM PoC platform CLI. Submit tasks, list Runs, inspect status, "
        "tail events, and replay completed Runs."
    ),
    no_args_is_help=True,
    add_completion=False,
    cls=_ScopeGuardGroup,
)

# ---------------------------------------------------------------------------
# Validation constants
# ---------------------------------------------------------------------------

# REQ 4.2: description bounds (1..8192 ASCII characters).
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

# Submit progress heartbeat. Milestone events are printed immediately;
# this heartbeat keeps long LLM waits from looking stuck.
_PROGRESS_HEARTBEAT_SECONDS: float = 30.0

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

_ROLE_DISPLAY_ORDER: tuple[str, ...] = (
    "S5_POLICY",
    "S4_SCANNER",
    "S3_ALLOCATOR",
    "S3STAR_AUDITOR",
    "S2_COORDINATOR",
    "S1_WORKER",
)

_FAILURE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "system_instantiation_failed",
        "llm_timeout",
        "llm_error",
        "sub_agent_error",
        "s1_instantiation_error",
        "event_log_append_error",
    }
)

_RUN_ID_EXAMPLE = "run-1234567890abcdef1234567890abcdef"
_REQ_REF_RE = re.compile(r"\s*\(REQ [^)]+\)")


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


def _clean_error_text(text: object) -> str:
    return _single_line(_REQ_REF_RE.sub("", str(text)))


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


def _validate_description(description: str) -> None:
    """REQ 4.2: enforce ``1 <= len(description) <= 8192`` and ASCII-only.

    On violation, writes the canonical message to stderr and exits with
    code 2. The message intentionally references both bounds and the
    ASCII constraint so that downstream log-greppers can match either
    failure mode against a single regex.
    """
    length = len(description)
    if (
        length < _DESCRIPTION_MIN
        or length > _DESCRIPTION_MAX
        or not description.isascii()
    ):
        if length < _DESCRIPTION_MIN:
            message = "Task description cannot be empty."
        elif length > _DESCRIPTION_MAX:
            message = (
                f"Task description length is {length}; the maximum is "
                f"{_DESCRIPTION_MAX} ASCII characters."
            )
        else:
            message = (
                "Task description must use ASCII characters. "
                "Japanese or other non-ASCII text is not accepted here yet."
            )
        _emit_cli_error(
            message,
            example='vsm submit "Write a short architecture summary"',
            next_step="Rewrite the description as 1 to 8192 ASCII characters.",
        )
        raise typer.Exit(code=2)


def _read_context_file(path: Path) -> str:
    """REQ 4.5: validate ``path`` and return its UTF-8 decoded contents.

    A file is rejected (exit code 2) if it does not exist, exceeds 1 MiB,
    or is not valid UTF-8. The reason string in the stderr message is
    stable so callers can pattern-match on it.
    """
    if not path.exists():
        _emit_cli_error(
            f"Context file does not exist: {path}",
            example='vsm submit "Review this note" --file notes.txt',
            next_step="Check the path, then pass an existing UTF-8 text file.",
        )
        raise typer.Exit(code=2)
    try:
        size = path.stat().st_size
    except OSError as exc:
        # ``stat`` after a successful ``exists`` should not normally fail
        # in production, but we handle it defensively to keep CLI errors
        # uniform.
        _emit_cli_error(
            f"Cannot inspect context file: {path}",
            next_step=f"Check the file permissions and try again. Details: {exc}",
        )
        raise typer.Exit(code=2) from None
    if size > _FILE_MAX_BYTES:
        _emit_cli_error(
            f"Context file exceeds {_FILE_MAX_BYTES} bytes: {path}",
            next_step="Use a smaller UTF-8 text file or split the context across smaller files.",
        )
        raise typer.Exit(code=2)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        _emit_cli_error(
            f"Context file is not valid UTF-8: {path}",
            next_step="Save the file as UTF-8 text, then run the command again.",
        )
        raise typer.Exit(code=2) from None
    except OSError as exc:
        _emit_cli_error(
            f"Could not read context file: {path}",
            next_step=f"Check the file permissions and try again. Details: {exc}",
        )
        raise typer.Exit(code=2) from None


def _run_id_validation_message(run_id: str) -> str:
    if not run_id:
        return "Run id cannot be empty."
    if len(run_id) > 64:
        return f"Run id is {len(run_id)} characters; the maximum is 64 ASCII characters."
    if not run_id.isascii():
        return "Run id must contain only ASCII characters."
    return "Run id is not valid."


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
        _emit_cli_error(
            _run_id_validation_message(run_id),
            example=f"vsm status {_RUN_ID_EXAMPLE}",
            next_step=(
                "Use a run_id printed by vsm submit or shown by vsm runs."
            ),
        )
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
            example=f"vsm status {_RUN_ID_EXAMPLE}",
            next_step=(
                "Run vsm runs to see available run ids, or check that "
                f"runs/{run_id}/events.jsonl exists."
            ),
        )
        raise typer.Exit(code=2)
    return path


@dataclass(frozen=True)
class _RunSummary:
    run_id: str
    short_run_id: str
    started_at: str
    state: str
    event_count: int
    task_description: str
    active: bool
    sort_mtime: float


def _short_id(value: str, visible: int = 8) -> str:
    """Return a stable, readable prefix for long UUID-like identifiers."""
    if len(value) <= visible:
        return value
    if value.startswith("run-"):
        return value[: 4 + visible]
    return value[:visible]


def _single_line(text: object) -> str:
    return " ".join(str(text).split())


def _short_text(text: object, width: int) -> str:
    line = _single_line(text)
    if len(line) <= width:
        return line
    if width <= 3:
        return line[:width]
    return f"{line[: width - 3]}..."


def _format_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "-"
    if "T" in value:
        value = value.replace("T", " ", 1)
    if value.endswith(".000Z"):
        value = f"{value[:-5]}Z"
    return value


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _read_events(path: Path) -> list[dict[str, Any]]:
    from vsm.eventlog.reader import read_all

    return read_all(path)


def _event_types(events: list[dict[str, Any]]) -> set[str]:
    return {
        event_type
        for evt in events
        if isinstance(event_type := evt.get("event_type"), str)
    }


def _derive_task_state(
    current_state: object,
    events: list[dict[str, Any]],
) -> tuple[str, str]:
    """Derive a useful Task state from existing events without schema changes."""
    state = current_state if isinstance(current_state, str) else "submitted"
    if state and state != "submitted":
        return state, "task_state_changed"

    types = _event_types(events)
    if "s1_completion" in types:
        return "completed", "s1_completion"
    failures = sorted(types & _FAILURE_EVENT_TYPES)
    if failures:
        return "failed", failures[0]
    if "s1_assignment_sent" in types or "s1_instantiated" in types:
        return "executing", "s1_assignment"
    if "policy_decision" in types:
        return "allocating", "policy_decision"
    if "s4_assessment_produced" in types:
        return "scanning", "s4_assessment"
    if "task_submitted" in types:
        return "submitted", "task_submitted"
    return state or "unknown", "events"


def _run_started_at(events: list[dict[str, Any]]) -> str:
    for evt in events:
        if evt.get("event_type") == "task_submitted":
            payload = evt.get("payload", {}) or {}
            submitted_at = payload.get("submitted_at")
            if isinstance(submitted_at, str):
                return submitted_at
    for evt in events:
        ts = evt.get("ts")
        if isinstance(ts, str):
            return ts
    return "-"


def _role_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
    system_id, info = item
    role = info.get("role", "")
    try:
        index = _ROLE_DISPLAY_ORDER.index(role)
    except ValueError:
        index = len(_ROLE_DISPLAY_ORDER)
    return index, system_id


def _progress_for_event(evt: dict[str, Any]) -> tuple[str, str] | None:
    event_type = evt.get("event_type")
    payload = evt.get("payload", {}) or {}
    if event_type == "system_instantiated":
        role = payload.get("role")
        if isinstance(role, str):
            return role, "System ready"
        return None
    if event_type == "task_submitted":
        return "S4_SCANNER", "Task accepted"
    if event_type == "s4_assessment_produced":
        return "S4_SCANNER", "Assessment produced"
    if event_type == "policy_decision":
        return "S5_POLICY", "Policy decision produced"
    if event_type == "s1_instantiated":
        return "S3_ALLOCATOR", "S1 worker created"
    if event_type == "s1_assignment_sent":
        return "S3_ALLOCATOR", "Work assigned to S1"
    if event_type == "s1_completion":
        return "S1_WORKER", "Work completed"
    if event_type == "audit_observation":
        return "S3STAR_AUDITOR", "Audit observation"
    if event_type == "audit_finding":
        return "S3STAR_AUDITOR", "Audit finding"
    if event_type == "audit_report_sent":
        return "S3STAR_AUDITOR", "Audit report sent"
    if isinstance(event_type, str) and event_type in _FAILURE_EVENT_TYPES:
        return "ERROR", event_type
    return None


def _emit_progress(
    *,
    clock: SystemClock,
    started: float,
    phase: str,
    message: str,
) -> None:
    elapsed = _format_elapsed(clock.monotonic() - started)
    typer.echo(f"[{elapsed}] {phase}: {message}", err=True)


def _summarise_run_dir(run_dir: Path) -> _RunSummary:
    events_path = run_dir / "events.jsonl"
    active = (run_dir / "RUNNING").exists()
    sort_mtime = run_dir.stat().st_mtime
    events = _read_events(events_path)
    from vsm.eventlog.replay import replay as replay_events

    state = replay_events(events_path)
    task_description = ""
    task_state = "no_task"
    if state.tasks:
        _task_id, info = next(iter(state.tasks.items()))
        task_description = _short_text(info.get("description", ""), 72)
        task_state, _source = _derive_task_state(info.get("state"), events)
    return _RunSummary(
        run_id=run_dir.name,
        short_run_id=_short_id(run_dir.name),
        started_at=_format_timestamp(_run_started_at(events)),
        state=task_state,
        event_count=len(events),
        task_description=task_description,
        active=active,
        sort_mtime=sort_mtime,
    )


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _event_summary(evt: dict[str, Any]) -> str | None:
    event_type = evt.get("event_type")
    payload = evt.get("payload", {}) or {}
    if not isinstance(payload, dict):
        return None

    if event_type == "task_submitted":
        description = payload.get("description")
        if isinstance(description, str):
            return f"task: {_short_text(description, 96)}"

    if event_type == "llm_invocation":
        response = payload.get("response")
        if isinstance(response, str):
            return f"llm response: {_short_text(response, 96)}"

    if event_type == "llm_timeout":
        elapsed_ms = payload.get("elapsed_ms")
        return f"error: LLM call timed out after {elapsed_ms} ms"

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
    }:
        reason = payload.get("reason")
        if isinstance(reason, str):
            return f"error: {_short_text(reason, 96)}"

    if event_type == "channel_rejected":
        channel = payload.get("channel")
        sender = payload.get("sender")
        receiver = payload.get("receiver")
        return f"error: channel rejected {sender} -> {receiver} on {channel}"

    if event_type == "s1_completion":
        result = payload.get("result")
        if isinstance(result, dict):
            success = result.get("success")
            text = result.get("text")
            if isinstance(text, str):
                return f"result: success={success} {_short_text(text, 96)}"
            return f"result: {_short_text(json.dumps(result, ensure_ascii=False, sort_keys=True), 96)}"

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

    return None


def _replay_line(evt: dict[str, Any]) -> str:
    ts = evt.get("ts", "-")
    payload = evt.get("payload", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    # ``system_id`` is the canonical lifecycle field; fall back to
    # ``sender`` for Channel events, which carry sender/receiver but
    # no system_id. ``-`` renders for events without either (e.g.
    # ``policy_decision``, ``task_submitted``).
    sys_id = payload.get("system_id") or payload.get("sender") or "-"
    channel = payload.get("channel") or "-"
    event_type = evt.get("event_type", "-")
    return f"{ts} {sys_id} {channel} {event_type}"


# ---------------------------------------------------------------------------
# submit (Task 19.1)
# ---------------------------------------------------------------------------


@app.command()
def submit(
    description: str = typer.Argument(
        ...,
        help="Task description, 1..8192 ASCII characters.",
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
                f"Run {run_id} did not finish within {_RUN_TIMEOUT_SECONDS:.0f} seconds.",
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
        detail = _clean_error_text(exc.detail)
        if "LLM provider" in detail:
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
                summary.task_description,
            )
        )

    id_width = max(len("RUN ID"), *(len(row[0]) for row in rows))
    started_width = max(len("STARTED"), *(len(row[1]) for row in rows))
    state_width = max(len("STATE"), *(len(row[2]) for row in rows))
    events_width = max(len("EVENTS"), *(len(row[3]) for row in rows))
    typer.echo("Run list (newest first)")
    typer.echo(
        f"{'RUN ID':<{id_width}}  "
        f"{'STARTED':<{started_width}}  "
        f"{'STATE':<{state_width}}  "
        f"{'EVENTS':>{events_width}}  "
        "TASK"
    )
    for display_id, started_at, state, event_count, task_description in rows:
        typer.echo(
            f"{display_id:<{id_width}}  "
            f"{started_at:<{started_width}}  "
            f"{state:<{state_width}}  "
            f"{event_count:>{events_width}}  "
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
