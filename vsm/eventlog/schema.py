"""Event_Log JSONL envelope and per-``event_type`` payload schemas.

This module defines the pydantic v2 models used by the Event_Log writer to
validate every record appended to ``runs/{run_id}/events.jsonl`` before it is
serialised. The shape follows design.md §Event_Log Writer / §Data Models
§Event スキーマ:

* a common envelope (:class:`Event`) carries ``ts``, ``run_id``,
  ``event_type``, ``seq`` and a free-form ``payload`` dict;
* each of the 26 ``event_type`` strings enumerated in design.md has its own
  pydantic payload model so that the writer can fail fast on malformed data
  rather than emitting a corrupt JSONL line;
* :data:`PAYLOAD_MODELS` maps every ``event_type`` to its payload class so
  that callers (and the writer in particular) can validate payloads through a
  single uniform entry point: :func:`validate_event_payload`.

Validates Requirements
----------------------
- REQ 10.7: every appended JSON object carries a UTC ISO 8601 timestamp with
  millisecond precision, an ``event_type`` identifier, and a Run identifier.
  The :class:`Event` envelope encodes these constraints with a regex pattern
  for ``ts``, a length-bounded ``run_id`` and an ``EventType`` :data:`Literal`
  that admits only the 26 known event types.
- REQ 10.5: state changes (``task_state_changed`` and the various
  ``*_instantiated`` / ``*_completion`` events) are recorded with sufficient
  payload structure for replay to reconstruct the cached runtime state.
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vsm.ids import generate_uuid

__all__ = [
    "EventType",
    "EVENT_TYPES",
    "EVENT_TYPES_V1",
    "KNOWN_EVENT_TYPES",
    "Event",
    "SystemInstantiatedPayload",
    "SystemInstantiationFailedPayload",
    "TaskSubmittedPayload",
    "TaskStateChangedPayload",
    "ChannelMessagePayload",
    "ChannelRejectedPayload",
    "LLMInvocationPayload",
    "LLMTimeoutPayload",
    "LLMErrorPayload",
    "S4AssessmentProducedPayload",
    "SubAgentErrorPayload",
    "DeliveryErrorPayload",
    "PolicyDecisionPayload",
    "DispatchErrorPayload",
    "S1InstantiatedPayload",
    "S1InstantiationErrorPayload",
    "S1AssignmentSentPayload",
    "S1CompletionPayload",
    "CoordinationConflictPayload",
    "CoordinationDirectivePayload",
    "CoordinationAckPayload",
    "CoordinationAckMissingPayload",
    "AuditObservationPayload",
    "AuditFindingPayload",
    "AuditReportSentPayload",
    "EventLogAppendErrorPayload",
    "PAYLOAD_MODELS",
    "PAYLOAD_MODELS_V1",
    "KNOWN_PAYLOAD_MODELS",
    "validate_event_payload",
]


# REQ 10.7: ``ts`` must be UTC ISO 8601 with millisecond precision and a
# trailing ``Z``. The pattern is shared with ``vsm.clock.format_iso_ms`` so a
# value produced by the production clock validates by construction.
_TS_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"


# REQ 10.7 / design.md §Data Models §Event スキーマ: the closed set of 26
# ``event_type`` strings. Declared as a tuple (single source of truth) and
# re-used as the values of the ``EventType`` :data:`Literal` below.
EVENT_TYPES: tuple[str, ...] = (
    "system_instantiated",
    "system_instantiation_failed",
    "task_submitted",
    "task_state_changed",
    "channel_message",
    "channel_rejected",
    "llm_invocation",
    "llm_timeout",
    "llm_error",
    "s4_assessment_produced",
    "sub_agent_error",
    "delivery_error",
    "policy_decision",
    "dispatch_error",
    "s1_instantiated",
    "s1_instantiation_error",
    "s1_assignment_sent",
    "s1_completion",
    "coordination_conflict",
    "coordination_directive",
    "coordination_ack",
    "coordination_ack_missing",
    "audit_observation",
    "audit_finding",
    "audit_report_sent",
    "event_log_append_error",
)

# ``EVENT_TYPES`` intentionally remains the legacy 26-event public constant.
# The refactor document adds domain/control events for Node, authority and
# tool execution. They are exposed separately so old compatibility tests and
# consumers that expect the legacy set can continue to rely on it.
EVENT_TYPES_V1: tuple[str, ...] = (
    "node_created",
    "node_started",
    "node_idled",
    "node_suspended",
    "node_resumed",
    "node_completed",
    "node_terminated",
    "node_failed",
    "node_differentiated",
    "agent_attached",
    "spec_revised",
    "tool_invoked",
    "tool_completed",
    "tool_failed",
    "budget_consumed",
    "budget_exceeded",
    "quota_exhausted",
    "quota_resumed",
    "authority_granted",
    "authority_revised",
    "coordination_requested",
    "coordination_decided",
    "algedonic_raised",
    "algedonic_handled",
    "algedonic_human_notification",
    "consortium_convened",
    "consortium_statement",
    "consortium_waiting",
    "consortium_human_timeout",
    "consortium_aborted",
    "consortium_decided",
    "escalation_requested",
    "human_review_requested",
    "summary_generated",
    "artifact_created",
    "web_run_created",
    "web_run_state_changed",
    "web_generation_started",
    "web_generation_finished",
    "web_instruction_received",
    "web_run_renamed",
    "web_retry_started",
    "web_run_cancelled",
    "web_partial_result_accepted",
    "web_run_completed",
)

KNOWN_EVENT_TYPES: tuple[str, ...] = EVENT_TYPES + EVENT_TYPES_V1

# Kept as a named alias for callers that imported EventType historically.
EventType = str


class _StrictModel(BaseModel):
    """Base class for every payload model.

    ``extra="forbid"`` means an unknown payload field is a validation error
    rather than a silent passthrough. This is the conservative default chosen
    by design.md §Error Handling: malformed events should fail fast at
    ``append`` time so that the writer can surface a typed error to the
    calling System rather than emit a corrupt JSONL line (REQ 10.6 / 10.7).
    """

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """Common JSONL envelope for every Event_Log record.

    Validates Requirements
    ----------------------
    - REQ 10.7: ``ts`` is constrained to UTC ISO 8601 with millisecond
      precision and a trailing ``Z``; ``event_type`` is restricted to the
      closed set of 26 known types via :data:`EventType`; ``run_id`` is
      bounded to 1..64 ASCII characters per REQ 10.2 (which REQ 10.7
      transitively requires the writer to honour).
    - REQ 10.5: ``seq`` is a non-negative integer assigned by the single
      writer task; together with FIFO line ordering this gives replay a
      stable sort key so that cached state can be reconstructed in the same
      order it was originally produced.

    Note
    ----
    The ``payload`` field is intentionally typed as ``dict`` here. Per-event
    structural validation lives in :data:`PAYLOAD_MODELS` and is invoked by
    :func:`validate_event_payload`, which the writer calls before it
    serialises the envelope.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(
        default_factory=generate_uuid,
        min_length=1,
        description="Unique event identifier for v1 EventEnvelope.",
    )
    seq: int = Field(
        ge=0,
        description=(
            "Monotonically increasing sequence number assigned by the single "
            "writer task (REQ 10.5 / 10.8)."
        ),
    )
    run_id: str = Field(
        min_length=1,
        max_length=64,
        description="Run identifier, 1..64 ASCII characters (REQ 10.2 / 10.7).",
    )
    node_id: str | None = Field(
        default=None,
        description="Node that owns the event, when known.",
    )
    stream_id: str | None = Field(
        default=None,
        description="Consistency boundary stream identifier.",
    )
    stream_version: int = Field(
        default=0,
        ge=0,
        description="Version within stream_id after this event is appended.",
    )
    event_type: EventType = Field(
        description="Known legacy or v1 event type identifier.",
    )
    schema_version: int = Field(
        default=1,
        ge=1,
        description="Payload schema version.",
    )
    ts: str = Field(
        pattern=_TS_PATTERN,
        description=(
            "UTC ISO 8601 timestamp with millisecond precision and trailing "
            "'Z' (REQ 10.7)."
        ),
    )
    actor_type: str = Field(
        default="system",
        min_length=1,
        description="Actor class that caused the event.",
    )
    actor_id: str | None = Field(
        default=None,
        description="Concrete actor identifier, when known.",
    )
    correlation_id: str | None = Field(
        default=None,
        description="Business-flow correlation identifier.",
    )
    causation_id: str | None = Field(
        default=None,
        description="Direct predecessor event_id, when known.",
    )
    payload: dict[str, Any] = Field(
        description=(
            "Per-event-type payload object. Structural validation is handled "
            "by PAYLOAD_MODELS / validate_event_payload."
        ),
    )

    @field_validator("run_id")
    @classmethod
    def _run_id_must_be_ascii(cls, v: str) -> str:
        """REQ 10.2: ``run_id`` must contain only ASCII characters."""
        if not v.isascii():
            raise ValueError(
                "run_id must contain only ASCII characters (REQ 10.2)"
            )
        return v

    @field_validator("event_type")
    @classmethod
    def _event_type_must_be_known(cls, v: str) -> str:
        if v not in KNOWN_EVENT_TYPES:
            raise ValueError(
                f"unknown event_type {v!r}; expected one of "
                f"{sorted(KNOWN_EVENT_TYPES)}"
            )
        return v


class LegacyEvent(BaseModel):
    """Compatibility shape documented by the original PoC.

    The production writer emits :class:`Event`, but this model is useful for
    tooling that deliberately wants to validate the old five-field contract.
    """

    model_config = ConfigDict(extra="forbid")

    ts: str = Field(pattern=_TS_PATTERN)
    run_id: str = Field(
        min_length=1,
        max_length=64,
    )
    event_type: EventType
    seq: int = Field(ge=0)
    payload: dict[str, Any]

    @field_validator("run_id")
    @classmethod
    def _run_id_must_be_ascii(cls, v: str) -> str:
        """REQ 10.2: ``run_id`` must contain only ASCII characters."""
        if not v.isascii():
            raise ValueError(
                "run_id must contain only ASCII characters (REQ 10.2)"
            )
        return v


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------


class SystemInstantiatedPayload(_StrictModel):
    """``system_instantiated`` payload (REQ 1.5, 1.6).

    Emitted at Run start when each of the five mandatory Systems comes up,
    and again whenever S3 dynamically instantiates a new S1_Worker.
    """

    system_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    sub_agent_count: int = Field(ge=0)


class SystemInstantiationFailedPayload(_StrictModel):
    """``system_instantiation_failed`` payload (REQ 1.7, 13.2).

    Emitted when a mandatory System cannot be brought up at Run start; the
    Run is then aborted with a non-zero exit code per REQ 13.2.
    """

    role: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class TaskSubmittedPayload(_StrictModel):
    """``task_submitted`` payload (REQ 4.6).

    Emitted by the CLI ``submit`` command after it accepts a Task and
    assigns a UUIDv4 ``task_id`` and Run identifier.
    """

    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1)
    file_paths: list[str] = Field(default_factory=list)
    submitted_at: str = Field(pattern=_TS_PATTERN)


class TaskStateChangedPayload(_StrictModel):
    """``task_state_changed`` payload (REQ 10.5).

    Emitted on every transition of ``Task.state``. Both ``from_state`` and
    ``to_state`` are the string values of :class:`vsm.runtime.TaskState`.
    """

    task_id: str = Field(min_length=1)
    from_state: str = Field(min_length=1)
    to_state: str = Field(min_length=1)


class ChannelMessagePayload(_StrictModel):
    """``channel_message`` payload (REQ 2.9).

    Emitted by the Message_Bus on successful delivery. The inner ``payload``
    field carries the System-defined message body, and is intentionally a
    free-form dict here.
    """

    sender: str = Field(min_length=1)
    receiver: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    payload: dict[str, Any]


class ChannelRejectedPayload(_StrictModel):
    """``channel_rejected`` payload (REQ 2.7, 2.8).

    Emitted by the Message_Bus when a sender attempts to use a channel that
    is not in the static ``ALLOWED_ROUTES`` table.
    """

    sender: str = Field(min_length=1)
    receiver: str = Field(min_length=1)
    channel: str = Field(min_length=1)


class LLMInvocationPayload(_StrictModel):
    """``llm_invocation`` payload (REQ 3.3).

    Emitted by :class:`LLM_Provider_Abstraction` after a successful
    completion: it captures the prompt, the response, and the latency /
    token accounting required for SLA verification.
    """

    system_id: str = Field(min_length=1)
    sub_agent_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt: str
    response: str
    latency_ms: int = Field(ge=0)
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    tokens_cache_read: int = Field(default=0, ge=0)
    backend: str | None = None
    session_ref: str | None = None


class LLMTimeoutPayload(_StrictModel):
    """``llm_timeout`` payload (REQ 3.5).

    Emitted when an LLM invocation exceeds the 60 second deadline.
    """

    system_id: str = Field(min_length=1)
    sub_agent_id: str = Field(min_length=1)
    elapsed_ms: int = Field(ge=0)
    backend: str | None = None
    session_ref: str | None = None
    tokens_cache_read: int = Field(default=0, ge=0)


class LLMErrorPayload(_StrictModel):
    """``llm_error`` payload (REQ 3.6).

    Emitted when the LLM provider returns a non-success response.
    ``provider_code`` and ``provider_message`` carry the upstream error
    classification verbatim for replay-time analysis.
    """

    system_id: str = Field(min_length=1)
    sub_agent_id: str = Field(min_length=1)
    provider_code: str = Field(min_length=1)
    provider_message: str
    backend: str | None = None
    session_ref: str | None = None
    tokens_cache_read: int = Field(default=0, ge=0)


class S4AssessmentProducedPayload(_StrictModel):
    """``s4_assessment_produced`` payload (REQ 5.2, 5.3).

    Emitted when S4_Scanner finishes assessing a Task and produces an
    opportunity / threat list for S5_Policy.
    """

    assessment_id: str = Field(min_length=1)
    opportunities: list[str] = Field(default_factory=list)
    threats: list[str] = Field(default_factory=list)


class SubAgentErrorPayload(_StrictModel):
    """``sub_agent_error`` payload (REQ 5.5).

    Emitted when a Sub_Agent overruns the 30 second SLA or otherwise fails.
    """

    sub_agent_id: str = Field(min_length=1)
    elapsed_ms: int = Field(ge=0)
    reason: str = Field(min_length=1)


class DeliveryErrorPayload(_StrictModel):
    """``delivery_error`` payload (REQ 5.6).

    Emitted on a delivery failure during S4 / S5 dispatch retries.
    """

    attempt: int = Field(ge=1)
    channel: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class PolicyDecisionPayload(_StrictModel):
    """``policy_decision`` payload (REQ 6.6).

    Emitted by S5_Policy when it decides on a directive (and optional
    follow-up request) in response to an S4 assessment or an S3* finding.
    """

    decision_id: str = Field(min_length=1)
    assessment_id: str = Field(min_length=1)
    directive: str = Field(min_length=1)
    followup_request: str | None = None


class DispatchErrorPayload(_StrictModel):
    """``dispatch_error`` payload (REQ 6.5).

    Emitted by S5_Policy when one branch of its concurrent dispatch (e.g.
    to S3 or to S4) fails while the other succeeds.
    """

    recipient: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class S1InstantiatedPayload(_StrictModel):
    """``s1_instantiated`` payload (REQ 7.4).

    Emitted by S3_Allocator after it spins up a new S1_Worker for a
    specialization that has no idle worker available.
    """

    s1_id: str = Field(min_length=1)
    specialization: str = Field(min_length=1)
    initial_assignment: str = Field(min_length=1)


class S1InstantiationErrorPayload(_StrictModel):
    """``s1_instantiation_error`` payload (REQ 7.5).

    Emitted when S1_Worker creation fails (e.g. configuration or resource
    exhaustion); the Task is then routed to the failure handler.
    """

    specialization: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class S1AssignmentSentPayload(_StrictModel):
    """``s1_assignment_sent`` payload (REQ 7.7).

    Emitted by S3_Allocator on every assignment it dispatches to an S1.
    """

    s1_id: str = Field(min_length=1)
    work_item_id: str = Field(min_length=1)
    assignment: dict[str, Any]


class S1CompletionPayload(_StrictModel):
    """``s1_completion`` payload (REQ 7.8, 12.4).

    Emitted by an S1_Worker on completion of a work item. The ``result``
    field is a free-form dict to admit per-specialization completion
    structures while still being JSON-serialisable.
    """

    s1_id: str = Field(min_length=1)
    work_item_id: str = Field(min_length=1)
    result: dict[str, Any]


class CoordinationConflictPayload(_StrictModel):
    """``coordination_conflict`` payload (REQ 8.7).

    Emitted by S2_Coordinator when it detects ``|s1_ids| >= 2`` workers of
    the same specialization claiming the same ``work_item_id``.
    """

    specialization: str = Field(min_length=1)
    work_item_id: str = Field(min_length=1)
    s1_ids: list[str] = Field(min_length=2)


class CoordinationDirectivePayload(_StrictModel):
    """``coordination_directive`` payload (REQ 8.7).

    Emitted by S2_Coordinator when it issues a coordination directive to
    resolve a detected conflict.
    """

    directive_id: str = Field(min_length=1)
    affected_s1_ids: list[str] = Field(min_length=1)
    directive: str = Field(min_length=1)


class CoordinationAckPayload(_StrictModel):
    """``coordination_ack`` payload (REQ 8.7).

    Emitted by an S1 when it acknowledges receipt of a coordination
    directive from S2_Coordinator.
    """

    directive_id: str = Field(min_length=1)
    s1_id: str = Field(min_length=1)


class CoordinationAckMissingPayload(_StrictModel):
    """``coordination_ack_missing`` payload (REQ 8.6).

    Emitted by S2_Coordinator when an S1 fails to acknowledge a directive
    within the 30 second deadline.
    """

    directive_id: str = Field(min_length=1)
    s1_id: str = Field(min_length=1)
    elapsed_ms: int = Field(ge=0)


class AuditObservationPayload(_StrictModel):
    """``audit_observation`` payload (REQ 9.2).

    Emitted by S3*_Auditor on each audit cycle. ``observed_state`` carries
    the snapshot of S1 state that the auditor inspected.
    """

    s1_id: str = Field(min_length=1)
    observed_state: dict[str, Any]


class AuditFindingPayload(_StrictModel):
    """``audit_finding`` payload (REQ 9.4).

    Emitted by S3*_Auditor when an audit observation produces a finding to
    be escalated to S5_Policy.
    """

    finding_id: str = Field(min_length=1)
    s1_id: str = Field(min_length=1)
    content: str = Field(min_length=1)


class AuditReportSentPayload(_StrictModel):
    """``audit_report_sent`` payload (REQ 9.6).

    Emitted by S3*_Auditor when it has delivered a finding to S5_Policy.
    """

    finding_id: str = Field(min_length=1)


class EventLogAppendErrorPayload(_StrictModel):
    """``event_log_append_error`` payload (REQ 10.6).

    Emitted (via a best-effort fallback path) when 3 successive append
    attempts for some other event have all failed; ``target_event_type`` is
    the event type that could not be appended.
    """

    target_event_type: str = Field(min_length=1)
    attempts: int = Field(ge=1)
    reason: str = Field(min_length=1)


class GenericV1Payload(BaseModel):
    """Permissive payload model for new v1 domain/control events.

    Subsystems define stronger typed contracts at their own boundary. The
    Event_Log layer only requires that payloads are JSON objects.
    """

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Registry and helper
# ---------------------------------------------------------------------------


# Mapping from ``event_type`` -> payload model class. Used by the writer (and
# by tests) to look up the correct model for runtime validation. The dict is
# ``Mapping[str, type[BaseModel]]`` so callers cannot accidentally mutate it.
PAYLOAD_MODELS: Mapping[str, type[BaseModel]] = {
    "system_instantiated": SystemInstantiatedPayload,
    "system_instantiation_failed": SystemInstantiationFailedPayload,
    "task_submitted": TaskSubmittedPayload,
    "task_state_changed": TaskStateChangedPayload,
    "channel_message": ChannelMessagePayload,
    "channel_rejected": ChannelRejectedPayload,
    "llm_invocation": LLMInvocationPayload,
    "llm_timeout": LLMTimeoutPayload,
    "llm_error": LLMErrorPayload,
    "s4_assessment_produced": S4AssessmentProducedPayload,
    "sub_agent_error": SubAgentErrorPayload,
    "delivery_error": DeliveryErrorPayload,
    "policy_decision": PolicyDecisionPayload,
    "dispatch_error": DispatchErrorPayload,
    "s1_instantiated": S1InstantiatedPayload,
    "s1_instantiation_error": S1InstantiationErrorPayload,
    "s1_assignment_sent": S1AssignmentSentPayload,
    "s1_completion": S1CompletionPayload,
    "coordination_conflict": CoordinationConflictPayload,
    "coordination_directive": CoordinationDirectivePayload,
    "coordination_ack": CoordinationAckPayload,
    "coordination_ack_missing": CoordinationAckMissingPayload,
    "audit_observation": AuditObservationPayload,
    "audit_finding": AuditFindingPayload,
    "audit_report_sent": AuditReportSentPayload,
    "event_log_append_error": EventLogAppendErrorPayload,
}

PAYLOAD_MODELS_V1: Mapping[str, type[BaseModel]] = {
    event_type: GenericV1Payload for event_type in EVENT_TYPES_V1
}

KNOWN_PAYLOAD_MODELS: Mapping[str, type[BaseModel]] = {
    **PAYLOAD_MODELS,
    **PAYLOAD_MODELS_V1,
}


def validate_event_payload(event_type: str, payload: dict[str, Any]) -> BaseModel:
    """Validate ``payload`` against the model registered for ``event_type``.

    This is the single entry point used by the Event_Log writer to enforce
    REQ 10.7 / REQ 10.5: no record is appended unless its envelope is
    well-formed *and* its payload matches the schema for the declared
    ``event_type``.

    Parameters
    ----------
    event_type : str
        The event type identifier. Must be one of :data:`EVENT_TYPES`.
    payload : dict
        The payload dict that will be embedded in the envelope's ``payload``
        field once validated.

    Returns
    -------
    BaseModel
        The parsed payload model instance.

    Raises
    ------
    ValueError
        If ``event_type`` is not registered in :data:`PAYLOAD_MODELS`.
    pydantic.ValidationError
        If ``payload`` does not satisfy the registered model's schema.
    """
    model_cls = KNOWN_PAYLOAD_MODELS.get(event_type)
    if model_cls is None:
        raise ValueError(
            f"unknown event_type {event_type!r}; expected one of "
            f"{sorted(KNOWN_PAYLOAD_MODELS)}"
        )
    return model_cls.model_validate(payload)


# Sanity check: keep ``EVENT_TYPES`` and ``PAYLOAD_MODELS`` in lockstep so a
# future contributor cannot add an event_type without also defining its
# payload schema.
assert set(EVENT_TYPES) == set(PAYLOAD_MODELS), (
    "EVENT_TYPES and PAYLOAD_MODELS must enumerate the same event_type set"
)
assert len(EVENT_TYPES) == 26, (
    f"design.md §Data Models §Event スキーマ defines exactly 26 event types; "
    f"got {len(EVENT_TYPES)}"
)

# ``from __future__ import annotations`` keeps every annotation as a string,
# which means pydantic cannot resolve the ``EventType`` Literal alias on
# ``Event`` until we hand it the module globals explicitly. Doing this once at
# import time is cheap and avoids any per-instance overhead.
Event.model_rebuild(_types_namespace=globals())
