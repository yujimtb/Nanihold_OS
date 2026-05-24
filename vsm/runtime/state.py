"""Replay-reconstructed runtime state.

This module defines the data structures that
:func:`vsm.eventlog.replay.replay` populates from ``events.jsonl``. It is
deliberately decoupled from the live Systems / Sub_Agent classes: the live
runtime is allowed to keep richer in-memory caches, but the *minimum* state
that must be reconstructible from the Event_Log alone is what
:class:`ReconstructedState` captures.

The four projections enumerated by REQ 10.10 are:

1. ``tasks`` — the set of Tasks and their current ``TaskState``.
2. ``s1_lifecycle`` — for every S1_Worker, the ordered history of lifecycle
   events (instantiation, assignments sent, completions).
3. ``channel_events`` — the ordered sequence of Message_Bus events
   (``channel_message`` / ``channel_rejected``).
4. ``audit_findings`` — the set of audit findings produced by S3*_Auditor.

A fifth field, :attr:`ReconstructedState.systems`, is tracked as a
convenience for the ``vsm status`` CLI subcommand (REQ 11.1) which needs the
``(system_id, sub_agent_count)`` tuples; it is *not* one of the four
projections that REQ 10.10 requires to round-trip.

Validates Requirements
----------------------
- REQ 10.1: every projection here is sourced exclusively from the
  Event_Log, so the live runtime never has to be consulted to produce the
  reconstructed state.
- REQ 10.9: the Python objects defined here are the canonical replay-side
  representation; runtime caches are allowed to be richer, but the replay
  output is authoritative.
- REQ 10.10: the four projections (``tasks``, ``s1_lifecycle``,
  ``channel_events``, ``audit_findings``) correspond element-by-element to
  the runtime cache projections referenced by Property 5 in design.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "TaskState",
    "S1LifecycleEvent",
    "ChannelEvent",
    "AuditFinding",
    "ReconstructedState",
]


class TaskState(Enum):
    """Lifecycle states of a :class:`vsm.runtime.Task`.

    The seven values mirror design.md §Data Models §Task / Run. They form a
    forward-only state machine: ``SUBMITTED`` is the initial state, and
    ``COMPLETED`` / ``FAILED`` are the terminal states. Intermediate states
    correspond to the System currently driving the Task (S4 is scanning, S5
    has produced a policy, S3 is allocating, S1 group is executing).
    """

    SUBMITTED = "submitted"
    SCANNING = "scanning"            # S4 is processing
    POLICY_PRODUCED = "policy_produced"
    ALLOCATING = "allocating"        # S3 is allocating S1 workers
    EXECUTING = "executing"          # S1 group is executing
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class S1LifecycleEvent:
    """A single lifecycle event for an S1_Worker.

    Captures one of the lifecycle transitions enumerated by REQ 1.6 / 7.4 /
    7.7 / 7.8 (instantiation, assignment dispatch, completion). Listed
    per-``s1_id`` in seq order on
    :attr:`ReconstructedState.s1_lifecycle`, which gives Property 5 a
    well-defined "ordered history" projection to compare against the live
    cache.

    Attributes
    ----------
    event_type : str
        One of ``"instantiated"`` | ``"assignment_sent"`` | ``"completion"``.
        This is the *normalised* lifecycle action, not the raw Event_Log
        ``event_type`` (which may be ``s1_instantiated``,
        ``system_instantiated`` with ``role == "S1_WORKER"``, etc.).
    seq : int
        The Event_Log ``seq`` at which the event occurred. Used as a stable
        sort key when callers need to interleave lifecycle events with
        :class:`ChannelEvent` records.
    payload : dict[str, Any]
        The original Event_Log payload, preserved verbatim so that callers
        can inspect specialisation / work_item_id / result without having
        to re-read the JSONL file.
    """

    event_type: str
    seq: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class ChannelEvent:
    """A single Message_Bus event observed on the Event_Log.

    Both successful deliveries (``channel_message``) and rejections
    (``channel_rejected``) are captured here so that REQ 10.10's "sequence
    of Channel events" projection round-trips for *both* outcomes. For
    rejections, ``payload`` is ``None`` because the rejected message is
    deliberately *not* persisted (REQ 2.7 / 2.8 only require that a
    ``channel_rejected`` event be appended; the inner payload is not part
    of the contract).

    Attributes
    ----------
    event_type : str
        Either ``"channel_message"`` or ``"channel_rejected"``.
    seq : int
        Event_Log sequence number; used to preserve append order in the
        ``channel_events`` projection.
    sender : str
        ``payload.sender`` — the sender System identifier.
    receiver : str
        ``payload.receiver`` — the receiver System identifier.
    channel : str
        ``payload.channel`` — the :class:`vsm.messaging.ChannelId` value.
    payload : dict[str, Any] | None
        For ``channel_message`` events, the inner application payload.
        For ``channel_rejected`` events, ``None``.
    """

    event_type: str
    seq: int
    sender: str
    receiver: str
    channel: str
    payload: dict[str, Any] | None


@dataclass(frozen=True)
class AuditFinding:
    """An audit finding produced by S3*_Auditor.

    Attributes
    ----------
    finding_id : str
        UUIDv4 finding identifier (REQ 9.4).
    s1_id : str
        Identifier of the S1_Worker the finding pertains to.
    content : str
        Human-readable finding content (REQ 9.4 / 9.5).
    seq : int
        Event_Log sequence number at which the finding was first observed;
        used for stable ordering when multiple findings share the same
        ``finding_id`` (which itself indexes the ``audit_findings`` projection).
    """

    finding_id: str
    s1_id: str
    content: str
    seq: int


@dataclass
class ReconstructedState:
    """Replay-reconstructed snapshot of a Run.

    The four required projections (REQ 10.10) are:

    * :attr:`tasks` — ``task_id -> {state, run_id, description, ...}``.
      ``state`` is the string value of :class:`TaskState` (e.g. ``"submitted"``);
      using strings rather than the enum keeps the projection JSON-equivalent
      to what the live runtime cache exposes for ``vsm status``.
    * :attr:`s1_lifecycle` — ``s1_id -> [S1LifecycleEvent...]`` in seq order.
    * :attr:`channel_events` — list of :class:`ChannelEvent` in seq order.
    * :attr:`audit_findings` — ``finding_id -> AuditFinding``.

    The optional :attr:`systems` field is *not* part of REQ 10.10 but is
    populated alongside ``s1_lifecycle`` to give the ``vsm status`` CLI
    subcommand (REQ 11.1) the ``(system_id, sub_agent_count)`` tuples it
    needs without forcing a separate replay pass.

    Validates Requirements
    ----------------------
    REQ 10.1, 10.9, 10.10. The four projections correspond
    element-by-element to the runtime cache projections referenced by
    Property 5 in design.md.
    """

    # REQ 10.10: ``task_id -> task fields`` projection. Stored as a plain
    # dict so callers can compare with the runtime cache via simple
    # ``set(d.items())`` equality.
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)

    # REQ 10.10: per-``s1_id`` ordered history of lifecycle events.
    s1_lifecycle: dict[str, list[S1LifecycleEvent]] = field(default_factory=dict)

    # REQ 10.10: ordered sequence of Channel events (deliveries + rejections).
    channel_events: list[ChannelEvent] = field(default_factory=list)

    # REQ 10.10: indexed set of audit findings.
    audit_findings: dict[str, AuditFinding] = field(default_factory=dict)

    # Convenience for ``vsm status`` (REQ 11.1); not part of REQ 10.10.
    systems: dict[str, dict[str, Any]] = field(default_factory=dict)
