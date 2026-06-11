"""Event_Log replay engine.

This module implements :func:`replay`, the reverse direction of
:class:`vsm.eventlog.writer.EventLogWriter`. Given a path to an
``events.jsonl`` file, it parses every line, applies each event to a fresh
:class:`vsm.runtime.state.ReconstructedState`, and returns the final state.

The single design constraint for replay is *order*: REQ 10.8 makes the
writer emit events in FIFO order, and REQ 10.10 requires that replay
recover the runtime cache element-by-element. We therefore sort by the
envelope's ``seq`` field before applying anything; the JSONL file is
*already* in seq order by construction (single-writer + append-only), but
the explicit sort makes replay robust to a future shuffler (e.g. a
log-rotation tool) and removes any reliance on the file's physical line
order.

Each :data:`vsm.eventlog.schema.EVENT_TYPES` member maps to an entry in the
internal ``_HANDLERS`` dispatch table; a handler that does not affect any
of the four REQ 10.10 projections is intentionally a no-op.

Validates Requirements
----------------------
- REQ 10.1: the Event_Log is the sole input; no live runtime state is
  consulted while replaying.
- REQ 10.9: the produced :class:`ReconstructedState` is the canonical
  authoritative reconstruction of the cached runtime state.
- REQ 10.10: the four projections (``tasks``, ``s1_lifecycle``,
  ``channel_events``, ``audit_findings``) are populated to round-trip with
  the runtime cache (Property 5).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from vsm.runtime.state import (
    AuditFinding,
    ChannelEvent,
    ReconstructedState,
    S1LifecycleEvent,
    TaskState,
)

__all__ = ["replay"]


# ---------------------------------------------------------------------------
# Per-event apply handlers
# ---------------------------------------------------------------------------
#
# Each handler takes ``(state, seq, payload)`` and mutates ``state`` in
# place. Handlers are intentionally narrow — they touch *only* the fields
# of :class:`ReconstructedState` that REQ 10.10 requires the corresponding
# event_type to populate. Events that are observability-only (e.g.
# ``llm_invocation``, ``llm_timeout``, ``event_log_append_error``) have no
# handler entry, which means ``_apply_event`` skips them silently — they
# are recorded on the Event_Log but they do not contribute to any of the
# four REQ 10.10 projections.


def _apply_system_instantiated(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """Track System lifecycle (REQ 1.5, 1.6, 11.1).

    Populates :attr:`ReconstructedState.systems` for every System
    instantiation so ``vsm status`` can list ``(system_id, sub_agent_count)``
    tuples without re-reading the file. When the role is ``S1_WORKER``,
    also seeds the ``s1_lifecycle`` projection with an ``"instantiated"``
    event so that S1 lifecycle history is present even when the writer
    chose to use ``system_instantiated`` rather than ``s1_instantiated``.
    """
    system_id = payload["system_id"]
    state.systems[system_id] = {
        "role": payload["role"],
        "sub_agent_count": payload["sub_agent_count"],
    }
    # REQ 10.10: any S1_Worker instantiation contributes to the
    # ``s1_lifecycle`` projection.
    if payload["role"] == "S1_WORKER":
        state.s1_lifecycle.setdefault(system_id, []).append(
            S1LifecycleEvent(
                event_type="instantiated", seq=seq, payload=dict(payload)
            )
        )


def _apply_s1_instantiated(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """REQ 7.4: record the dynamic S1_Worker creation in ``s1_lifecycle``."""
    state.s1_lifecycle.setdefault(payload["s1_id"], []).append(
        S1LifecycleEvent(
            event_type="instantiated", seq=seq, payload=dict(payload)
        )
    )


def _apply_s1_assignment_sent(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """REQ 7.7: record an assignment dispatch in ``s1_lifecycle``."""
    state.s1_lifecycle.setdefault(payload["s1_id"], []).append(
        S1LifecycleEvent(
            event_type="assignment_sent", seq=seq, payload=dict(payload)
        )
    )


def _apply_s1_completion(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """REQ 7.8: record a completion in ``s1_lifecycle``."""
    state.s1_lifecycle.setdefault(payload["s1_id"], []).append(
        S1LifecycleEvent(
            event_type="completion", seq=seq, payload=dict(payload)
        )
    )


def _apply_task_submitted(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """REQ 4.6 / 10.10: seed the ``tasks`` projection on submission."""
    state.tasks[payload["task_id"]] = {
        "state": TaskState.SUBMITTED.value,
        "run_id": payload["run_id"],
        "description": payload["description"],
        "file_paths": list(payload.get("file_paths", [])),
        "submitted_at": payload["submitted_at"],
    }


def _apply_task_state_changed(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """REQ 10.5 / 10.10: advance the ``tasks`` projection on transition.

    Replay tolerates ``task_state_changed`` events arriving for tasks that
    have not been seen yet (e.g. when a JSONL fragment was truncated and
    the corresponding ``task_submitted`` line is missing) by silently
    ignoring the transition; the alternative (raising) would be brittle in
    the face of partial logs and is *not* required by REQ 10.10.
    """
    task_id = payload["task_id"]
    if task_id in state.tasks:
        state.tasks[task_id]["state"] = payload["to_state"]


def _apply_channel_message(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """REQ 2.9 / 10.10: append to the ``channel_events`` projection."""
    state.channel_events.append(
        ChannelEvent(
            event_type="channel_message",
            seq=seq,
            sender=payload["sender"],
            receiver=payload["receiver"],
            channel=payload["channel"],
            payload=dict(payload["payload"]),
        )
    )


def _apply_channel_rejected(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """REQ 2.7 / 10.10: append a rejection to the ``channel_events`` projection.

    The rejected message body is intentionally dropped (REQ 2.7 / 2.8 only
    require that the rejection itself be recorded, not the offending
    payload) so :class:`ChannelEvent.payload` is set to ``None``.
    """
    state.channel_events.append(
        ChannelEvent(
            event_type="channel_rejected",
            seq=seq,
            sender=payload["sender"],
            receiver=payload["receiver"],
            channel=payload["channel"],
            payload=None,
        )
    )


def _apply_audit_finding(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """REQ 9.4 / 10.10: index the finding by ``finding_id``.

    Replay treats the *first* ``audit_finding`` event for a given
    ``finding_id`` as authoritative; subsequent events with the same
    identifier are ignored so that re-emitted findings (e.g. on a retry
    path) do not overwrite the seq of the original observation.
    """
    finding_id = payload["finding_id"]
    if finding_id not in state.audit_findings:
        state.audit_findings[finding_id] = AuditFinding(
            finding_id=finding_id,
            s1_id=payload["s1_id"],
            content=payload["content"],
            seq=seq,
        )


def _apply_tool_completed(
    state: ReconstructedState, seq: int, payload: dict[str, Any]
) -> None:
    """Cache completed Tool results for deterministic replay consumers."""
    invocation_id = payload["tool_invocation_id"]
    state.tool_results[invocation_id] = {
        "tool_name": payload["tool_name"],
        "result": dict(payload.get("result") or {}),
        "seq": seq,
    }


# Dispatch table: ``event_type`` -> handler. Events not listed here are
# either pure observability events (no impact on REQ 10.10 projections) or
# error/diagnostic events; in both cases the correct behaviour at replay
# time is to skip them.
_HANDLERS: dict[
    str, Callable[[ReconstructedState, int, dict[str, Any]], None]
] = {
    "system_instantiated": _apply_system_instantiated,
    "s1_instantiated": _apply_s1_instantiated,
    "s1_assignment_sent": _apply_s1_assignment_sent,
    "s1_completion": _apply_s1_completion,
    "task_submitted": _apply_task_submitted,
    "task_state_changed": _apply_task_state_changed,
    "channel_message": _apply_channel_message,
    "channel_rejected": _apply_channel_rejected,
    "audit_finding": _apply_audit_finding,
    "tool_completed": _apply_tool_completed,
}


def _apply_event(state: ReconstructedState, evt: dict[str, Any]) -> None:
    """Dispatch a single Event envelope to the handler registered for its type.

    Unknown / unhandled event types are silently ignored: REQ 10.10 only
    constrains the four projections, and any future event type that does
    *not* affect those projections is allowed to flow through untouched.
    """
    handler = _HANDLERS.get(evt["event_type"])
    if handler is None:
        return
    handler(state, evt["seq"], evt["payload"])


def replay(path: Path) -> ReconstructedState:
    """Replay an ``events.jsonl`` file into a :class:`ReconstructedState`.

    The function reads every non-empty line as a JSON object, sorts the
    resulting list by the envelope's ``seq`` field (REQ 10.8 guarantees the
    file is already seq-ordered by construction; the explicit sort makes
    replay robust to ad-hoc edits and to future log-rotation tooling),
    and applies each event to a fresh :class:`ReconstructedState`. The
    final state is returned to the caller, ready for the element-by-element
    equality check required by Property 5 / REQ 10.10.

    Parameters
    ----------
    path : pathlib.Path
        Path to the ``events.jsonl`` file. The caller is expected to have
        verified existence; ``replay`` does not handle missing-file errors
        because the CLI ``replay`` / ``status`` / ``tail`` subcommands
        translate that condition into the REQ 11.7 ``Event_Log not found``
        error themselves.

    Returns
    -------
    ReconstructedState
        The reconstructed state. Empty projections (e.g. an empty Run with
        no Tasks yet) round-trip as empty dicts / lists.

    Raises
    ------
    json.JSONDecodeError
        If any line in the file is not valid JSON. Replay deliberately
        does not swallow this — a corrupt JSONL line indicates either a
        writer bug or external tampering, and either way the caller should
        be told.
    """
    state = ReconstructedState()

    # REQ 10.8: read every line, skipping blank lines that may appear if
    # the writer was killed mid-flush. ``encoding="utf-8"`` matches the
    # writer's open mode so non-ASCII payloads (e.g. 営業 / リサーチ
    # Sub_Agent labels) round-trip cleanly.
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))

    # REQ 10.8 / 10.10: explicit seq-ordered sort. The file *should*
    # already be in seq order, but sorting defensively keeps replay
    # deterministic even for ad-hoc / merged log files.
    events.sort(key=lambda e: e["seq"])

    for evt in events:
        _apply_event(state, evt)

    return state
