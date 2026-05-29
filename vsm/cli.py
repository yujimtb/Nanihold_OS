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
import sys
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
        typer.echo(
            f"description length out of range "
            f"[{_DESCRIPTION_MIN}, {_DESCRIPTION_MAX}] ASCII",
            err=True,
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
        typer.echo(f"Event_Log not found for run {run_id}", err=True)
        raise typer.Exit(code=2)
    return path


# ---------------------------------------------------------------------------
# submit (Task 19.1)
# ---------------------------------------------------------------------------


@app.command()
def submit(
    description: str = typer.Argument(
        ...,
        help="Task description (1..8192 ASCII characters, REQ 4.2).",
    ),
    file: Optional[list[Path]] = typer.Option(
        None,
        "--file",
        "-f",
        help="Optional context file path (repeatable, REQ 4.3).",
    ),
) -> None:
    """Submit a Task and run it through the VSM platform (REQ 4.1〜4.7).

    Validates the description (REQ 4.2) and any ``--file`` arguments
    (REQ 4.5) synchronously, generates fresh UUIDv4 ``run_id`` and
    ``task_id`` identifiers (REQ 4.6), bootstraps the Platform via
    :func:`vsm.runtime.lifecycle.start_run`, appends the ``task_submitted``
    event, triggers S4_Scanner with the Task, and waits up to
    1800 seconds (REQ 12.9) for the Run to complete. On success the
    identifiers are written to stdout in the documented format
    (REQ 4.7); on failure the appropriate exit code is returned.
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
        try:
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
                if (
                    "s1_completion" in event_types
                    and _REQUIRED_COMPLETION_ROLES.issubset(roles_seen)
                ):
                    return

            # REQ 12.9: 1800 second deadline exceeded.
            typer.echo(
                f"run {run_id} timed out after {_RUN_TIMEOUT_SECONDS:.0f}s "
                "(REQ 12.9)",
                err=True,
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
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=3) from None
    except RunDirectoryError as exc:
        # REQ 10.4: ``runs/{run_id}/`` could not be created.
        typer.echo(str(exc), err=True)
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
    run_id: str = typer.Argument(..., help="Run identifier (REQ 10.2)."),
) -> None:
    """Print task and System summaries reconstructed from ``events.jsonl``.

    Validates Requirements: 10.2, 11.1, 11.7.

    For each Task in the reconstructed state, prints
    ``(task_id, state)``; for each System, prints
    ``(system_id, sub_agent_count)``. One row per line so the output is
    grep-friendly.
    """
    _validate_run_id_or_exit(run_id)
    path = _require_events_path(run_id)

    # Heavy import deferred until after argument validation.
    from vsm.eventlog.replay import replay

    state = replay(path)

    # REQ 11.1: tasks first, then systems. Each entry is a single-line
    # tuple-formatted record.
    for task_id, info in state.tasks.items():
        typer.echo(f"({task_id}, {info['state']})")
    for system_id, info in state.systems.items():
        typer.echo(f"({system_id}, {info['sub_agent_count']})")


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
    run_id: str = typer.Argument(..., help="Run identifier (REQ 10.2)."),
    system: Optional[list[str]] = typer.Option(
        None,
        "--system",
        "-s",
        help="Filter by sender/receiver/system_id (repeatable, OR).",
    ),
    channel: Optional[list[str]] = typer.Option(
        None,
        "--channel",
        "-c",
        help="Filter by channel id (repeatable, OR).",
    ),
) -> None:
    """Tail ``events.jsonl`` with optional ``--system`` / ``--channel`` filters.

    Validates Requirements: 10.2, 11.2, 11.3, 11.4, 11.7.

    Emits each matching event as a single-line JSON object on stdout.
    Ctrl-C terminates the tail cleanly with exit code 0.
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
    run_id: str = typer.Argument(..., help="Run identifier (REQ 10.2)."),
) -> None:
    """Replay ``events.jsonl`` in human-readable single-line format.

    Validates Requirements: 10.2, 11.5, 11.6, 11.7.

    Prints ``<ts> <system_id> <channel> <event_type>`` per event in
    append order. Fields that are not present on a given event type are
    rendered as ``-`` so every line has the same column structure.
    Active Runs (those whose ``RUNNING`` lockfile is still present) are
    flagged on stderr before the replay output is emitted (REQ 11.6).
    """
    _validate_run_id_or_exit(run_id)
    path = _require_events_path(run_id)

    # REQ 11.6: warn (on stderr, before any stdout output) if the Run is
    # still active. The lockfile is created by
    # :class:`vsm.runtime.lifecycle.Platform` at Run start and removed
    # by :meth:`Platform.shutdown`; observing it post-shutdown therefore
    # is a strong indicator the Run is still in flight.
    lockfile = Path("runs") / run_id / "RUNNING"
    if lockfile.exists():
        typer.echo(f"warning: run {run_id} is still active", err=True)

    # Heavy import deferred until after argument validation.
    from vsm.eventlog.reader import read_all

    events = read_all(path)
    for evt in events:
        ts = evt.get("ts", "-")
        payload = evt.get("payload", {}) or {}
        # ``system_id`` is the canonical lifecycle field; fall back to
        # ``sender`` for Channel events, which carry sender/receiver but
        # no system_id. ``-`` renders for events without either (e.g.
        # ``policy_decision``, ``task_submitted``).
        sys_id = payload.get("system_id") or payload.get("sender") or "-"
        channel = payload.get("channel") or "-"
        event_type = evt.get("event_type", "-")
        typer.echo(f"{ts} {sys_id} {channel} {event_type}")


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    app()
