"""Property 7 (Required field presence). Validates Requirements: 10.7, 10.2."""

from __future__ import annotations
import pytest
from hypothesis import given, settings, strategies as st
from pydantic import ValidationError
from vsm.eventlog.schema import (
    Event, EVENT_TYPES, validate_event_payload, PAYLOAD_MODELS
)


def _valid_ts(): return "2025-01-15T03:14:15.926Z"


@settings(max_examples=100)
@given(seq=st.integers(min_value=0, max_value=10**6))
def test_valid_envelope(seq):
    evt = Event(
        ts=_valid_ts(),
        run_id="run-abc",
        event_type="system_instantiated",
        seq=seq,
        payload={"system_id": "x", "role": "S1_WORKER", "sub_agent_count": 1},
    )
    assert evt.seq == seq


@settings(max_examples=100)
@given(bad_ts=st.text(min_size=1, max_size=30).filter(lambda s: not s.endswith("Z")))
def test_invalid_ts_rejected(bad_ts):
    with pytest.raises(ValidationError):
        Event(ts=bad_ts, run_id="run-abc", event_type="system_instantiated", seq=0,
              payload={"system_id": "x", "role": "S1_WORKER", "sub_agent_count": 1})


@settings(max_examples=100)
@given(over=st.text(alphabet=st.characters(min_codepoint=0, max_codepoint=127), min_size=65, max_size=200))
def test_run_id_over_64_rejected(over):
    with pytest.raises(ValidationError):
        Event(ts=_valid_ts(), run_id=over, event_type="system_instantiated", seq=0,
              payload={"system_id": "x", "role": "S1_WORKER", "sub_agent_count": 1})


def test_run_id_empty_rejected():
    with pytest.raises(ValidationError):
        Event(ts=_valid_ts(), run_id="", event_type="system_instantiated", seq=0,
              payload={"system_id": "x", "role": "S1_WORKER", "sub_agent_count": 1})


@settings(max_examples=100)
@given(non_ascii=st.text(alphabet=st.characters(min_codepoint=0x4E00, max_codepoint=0x9FFF), min_size=1, max_size=10))
def test_run_id_non_ascii_rejected(non_ascii):
    with pytest.raises(ValidationError):
        Event(ts=_valid_ts(), run_id=non_ascii, event_type="system_instantiated", seq=0,
              payload={"system_id": "x", "role": "S1_WORKER", "sub_agent_count": 1})


@settings(max_examples=100)
@given(neg=st.integers(max_value=-1))
def test_negative_seq_rejected(neg):
    with pytest.raises(ValidationError):
        Event(ts=_valid_ts(), run_id="run-x", event_type="system_instantiated", seq=neg,
              payload={"system_id": "x", "role": "S1_WORKER", "sub_agent_count": 1})


def test_unknown_event_type_rejected():
    with pytest.raises(ValidationError):
        Event(ts=_valid_ts(), run_id="run-x", event_type="not_an_event", seq=0, payload={})


def test_validate_event_payload_unknown_type():
    with pytest.raises(ValueError):
        validate_event_payload("not_an_event", {})


@pytest.mark.parametrize("event_type", list(EVENT_TYPES))
def test_payload_model_exists_for_each_event_type(event_type):
    assert event_type in PAYLOAD_MODELS


def test_event_types_count():
    assert len(EVENT_TYPES) == 26
