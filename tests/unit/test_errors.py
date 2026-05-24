"""Unit tests for the VSM_Platform exception hierarchy.

This module verifies the exception classes defined in :mod:`vsm.errors`
against design.md §Error Handling (例外階層 / Exit Code 体系).

Validates Requirements
----------------------
- REQ 1.7: Failed instantiation of a mandatory System aborts the Run with
  an Event_Log entry; covered structurally by
  :class:`~vsm.errors.SystemInstantiationError` and the structural
  guarantees of :class:`~vsm.errors.ConfigError`.
- REQ 2.7: ``ALLOWED_ROUTES`` violations must surface as a typed
  messaging rejection; covered by :class:`~vsm.errors.ChannelRejected`
  inheriting from :class:`~vsm.errors.MessagingError`.
- REQ 3.5: LLM 60-second timeouts must propagate as a typed error
  carrying the offending Sub_Agent identifier and elapsed duration;
  covered by :class:`~vsm.errors.LLMTimeoutError`.
- REQ 3.6: LLM provider errors must preserve provider-supplied code and
  message for ``llm_error`` payload transcription; covered by
  :class:`~vsm.errors.LLMProviderError`.
- REQ 4.2: CLI input validation violations exit with a non-zero exit
  code; covered structurally by :class:`~vsm.errors.CLIError` carrying
  ``exit_code``.
- REQ 4.5: ``--file`` validation failures share the CLI exit-code path;
  covered by :class:`~vsm.errors.CLIError`.
- REQ 13.2: Mandatory-System structural-constraint violations must
  surface a typed error listing the missing roles; covered by
  :class:`~vsm.errors.ConfigError`.
- REQ 14.8: Out-of-scope CLI capability requests must surface a typed
  error with exit code 5; covered structurally by
  :class:`~vsm.errors.CLIError` allowing arbitrary ``exit_code`` values.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from vsm.errors import (
    CLIError,
    ChannelRejected,
    ConfigError,
    CoordinationAckMissing,
    DispatchError,
    EventLogAppendError,
    EventLogError,
    LLMError,
    LLMProviderError,
    LLMTimeoutError,
    MessagingError,
    RunDirectoryError,
    SubAgentError,
    SystemInstantiationError,
    VSMError,
)


# ---------------------------------------------------------------------------
# Hierarchy / inheritance
# ---------------------------------------------------------------------------


ALL_EXCEPTION_CLASSES = [
    VSMError,
    ConfigError,
    CLIError,
    RunDirectoryError,
    MessagingError,
    ChannelRejected,
    LLMError,
    LLMTimeoutError,
    LLMProviderError,
    EventLogError,
    EventLogAppendError,
    SystemInstantiationError,
    DispatchError,
    SubAgentError,
    CoordinationAckMissing,
]


def test_all_fifteen_exception_classes_present() -> None:
    """The hierarchy must expose exactly the 15 documented classes."""

    assert len(ALL_EXCEPTION_CLASSES) == 15
    # Names must be unique.
    names = {cls.__name__ for cls in ALL_EXCEPTION_CLASSES}
    assert len(names) == 15


@pytest.mark.parametrize("cls", ALL_EXCEPTION_CLASSES)
def test_every_exception_inherits_from_vsm_error(cls: type[Exception]) -> None:
    """Every class in the hierarchy must be a subclass of :class:`VSMError`."""

    assert issubclass(cls, VSMError)
    assert issubclass(cls, Exception)


def test_messaging_subclasses_chain() -> None:
    """:class:`ChannelRejected` is a :class:`MessagingError` is a :class:`VSMError`."""

    assert issubclass(ChannelRejected, MessagingError)
    assert issubclass(MessagingError, VSMError)


def test_llm_subclasses_chain() -> None:
    """LLM-specific subclasses must descend from :class:`LLMError`."""

    assert issubclass(LLMTimeoutError, LLMError)
    assert issubclass(LLMProviderError, LLMError)
    assert issubclass(LLMError, VSMError)


def test_event_log_subclasses_chain() -> None:
    """:class:`EventLogAppendError` must descend from :class:`EventLogError`."""

    assert issubclass(EventLogAppendError, EventLogError)
    assert issubclass(EventLogError, VSMError)


# ---------------------------------------------------------------------------
# CLIError (REQ 4.2, 14.8)
# ---------------------------------------------------------------------------


def test_cli_error_default_exit_code_is_one() -> None:
    """The unclassified default exit code is 1 (design.md §Exit Code 体系)."""

    err = CLIError("boom")
    assert err.exit_code == 1
    assert str(err) == "boom"


def test_cli_error_stores_explicit_exit_code() -> None:
    """``exit_code=2`` covers REQ 4.2 (CLI input validation)."""

    err = CLIError("bad description", exit_code=2)
    assert err.exit_code == 2
    assert str(err) == "bad description"


def test_cli_error_supports_out_of_scope_exit_code() -> None:
    """``exit_code=5`` covers REQ 14.8 (scope-out rejection)."""

    err = CLIError("requested capability is out of MVP scope fsx", exit_code=5)
    assert err.exit_code == 5
    assert "fsx" in str(err)


def test_cli_error_exit_code_is_assignable() -> None:
    """The ``exit_code`` attribute must be writable for CLI plumbing."""

    err = CLIError("msg", exit_code=2)
    err.exit_code = 4
    assert err.exit_code == 4


def test_cli_error_can_be_raised_and_caught_as_vsm_error() -> None:
    with pytest.raises(VSMError) as excinfo:
        raise CLIError("input invalid", exit_code=2)
    assert isinstance(excinfo.value, CLIError)
    assert excinfo.value.exit_code == 2


# ---------------------------------------------------------------------------
# ConfigError (REQ 13.2, 13.3)
# ---------------------------------------------------------------------------


def test_config_error_stores_missing_roles_and_detail() -> None:
    err = ConfigError([], "no missing roles but invalid count")
    assert err.missing_roles == []
    assert err.detail == "no missing roles but invalid count"
    # Empty missing_roles → message is just the detail.
    assert str(err) == "no missing roles but invalid count"


def test_config_error_includes_missing_roles_in_message() -> None:
    err = ConfigError(["S2_COORDINATOR", "S5_POLICY"], "missing required systems")
    assert err.missing_roles == ["S2_COORDINATOR", "S5_POLICY"]
    assert err.detail == "missing required systems"
    msg = str(err)
    assert "S2_COORDINATOR" in msg
    assert "S5_POLICY" in msg
    assert "missing required systems" in msg


def test_config_error_missing_roles_is_copied_not_aliased() -> None:
    """Mutating the input list must not affect the stored attribute."""

    src = ["S4_SCANNER"]
    err = ConfigError(src, "incomplete config")
    src.append("S3_ALLOCATOR")
    assert err.missing_roles == ["S4_SCANNER"]


# ---------------------------------------------------------------------------
# LLMTimeoutError (REQ 3.5)
# ---------------------------------------------------------------------------


def test_llm_timeout_error_accepts_timedelta() -> None:
    elapsed = timedelta(seconds=60.123)
    err = LLMTimeoutError("agent-id", elapsed)
    assert err.sub_agent_id == "agent-id"
    assert err.elapsed == elapsed
    msg = str(err)
    assert "agent-id" in msg
    assert "60.123" in msg


def test_llm_timeout_error_accepts_float_seconds() -> None:
    err = LLMTimeoutError("agent-7", 30.5)
    assert err.sub_agent_id == "agent-7"
    assert err.elapsed == 30.5
    msg = str(err)
    assert "agent-7" in msg
    assert "30.500" in msg


def test_llm_timeout_error_accepts_int_seconds() -> None:
    """``int`` is accepted because ``int`` is implicitly compatible with float."""

    err = LLMTimeoutError("agent-int", 60)
    assert err.sub_agent_id == "agent-int"
    assert "60.000" in str(err)


# ---------------------------------------------------------------------------
# LLMProviderError (REQ 3.6)
# ---------------------------------------------------------------------------


def test_llm_provider_error_retains_int_code_and_message() -> None:
    err = LLMProviderError(429, "rate")
    assert err.code == 429
    assert err.message == "rate"
    msg = str(err)
    assert "429" in msg
    assert "rate" in msg


def test_llm_provider_error_retains_string_code() -> None:
    err = LLMProviderError("RATE_LIMIT", "too many requests")
    assert err.code == "RATE_LIMIT"
    assert err.message == "too many requests"
    msg = str(err)
    assert "RATE_LIMIT" in msg
    assert "too many requests" in msg


# ---------------------------------------------------------------------------
# EventLogAppendError (REQ 10.6)
# ---------------------------------------------------------------------------


def test_event_log_append_error_chains_via_dunder_cause() -> None:
    """The original ``OSError`` must be exposed via both ``cause`` and ``__cause__``."""

    cause = OSError("disk full")

    class _DummyEvent:
        event_type = "channel_message"

    event = _DummyEvent()
    err = EventLogAppendError(event=event, cause=cause)

    assert err.event is event
    assert err.cause is cause
    assert err.__cause__ is cause
    assert "channel_message" in str(err)
    assert "disk full" in str(err)


def test_event_log_append_error_falls_back_to_event_type_name() -> None:
    """Events without an ``event_type`` attribute should still produce a message."""

    cause = OSError("io")
    event = object()
    err = EventLogAppendError(event=event, cause=cause)

    assert err.event is event
    assert err.cause is cause
    assert err.__cause__ is cause
    # Falls back to ``type(event).__name__`` ("object").
    assert "object" in str(err)


def test_event_log_append_error_preserves_chain_when_raised_from() -> None:
    """``raise ... from ...`` must keep the cause attached."""

    cause = OSError("write failed")

    class _Evt:
        event_type = "policy_decision"

    with pytest.raises(EventLogAppendError) as excinfo:
        try:
            raise cause
        except OSError as oe:
            raise EventLogAppendError(event=_Evt(), cause=oe) from oe

    # ``__cause__`` is set both by the constructor and by ``raise ... from``.
    assert excinfo.value.__cause__ is cause
    assert excinfo.value.cause is cause


# ---------------------------------------------------------------------------
# Marker exception classes (raisable smoke checks)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls",
    [
        RunDirectoryError,
        MessagingError,
        ChannelRejected,
        LLMError,
        EventLogError,
        SystemInstantiationError,
        DispatchError,
        SubAgentError,
        CoordinationAckMissing,
        VSMError,
    ],
)
def test_marker_exceptions_are_raisable_with_message(cls: type[Exception]) -> None:
    """Marker exceptions take a single message arg and round-trip ``str``."""

    with pytest.raises(cls) as excinfo:
        raise cls("boom")
    assert str(excinfo.value) == "boom"


def test_channel_rejected_is_messaging_error_at_runtime() -> None:
    """A raised :class:`ChannelRejected` is catchable as :class:`MessagingError`."""

    with pytest.raises(MessagingError):
        raise ChannelRejected("S3* → S3_Allocator forbidden")
