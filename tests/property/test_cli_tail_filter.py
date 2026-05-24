"""Property 12 (Tail filter semantics). Validates Requirements: 11.2, 11.3, 11.4."""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from vsm.cli import _build_tail_predicate


# ---------------------------------------------------------------------------
# Sample event factories
# ---------------------------------------------------------------------------


def _evt_channel_msg(sender: str, receiver: str, channel: str) -> dict:
    """Build a minimal ``channel_message`` event used by the predicate tests."""
    return {
        "event_type": "channel_message",
        "payload": {
            "sender": sender,
            "receiver": receiver,
            "channel": channel,
            "payload": {},
        },
    }


def _evt_system_inst(system_id: str) -> dict:
    """Build a minimal ``system_instantiated`` event used by the predicate tests."""
    return {
        "event_type": "system_instantiated",
        "payload": {
            "system_id": system_id,
            "role": "S1_WORKER",
            "sub_agent_count": 1,
        },
    }


# ---------------------------------------------------------------------------
# Example-based assertions
# ---------------------------------------------------------------------------


def test_no_filters_passes_all() -> None:
    """REQ 11.4: empty filter set accepts every event."""
    pred = _build_tail_predicate([], [])
    assert pred(_evt_channel_msg("a", "b", "S1-S2")) is True
    assert pred(_evt_system_inst("sys-x")) is True


def test_system_filter_or_within() -> None:
    """REQ 11.3: multiple ``--system`` values combine with OR."""
    pred = _build_tail_predicate(["sys-a", "sys-b"], [])
    assert pred(_evt_system_inst("sys-a")) is True
    assert pred(_evt_system_inst("sys-b")) is True
    assert pred(_evt_system_inst("sys-c")) is False


def test_channel_filter() -> None:
    """REQ 11.3: ``--channel`` filter restricts to listed channels only."""
    pred = _build_tail_predicate([], ["S1-S2"])
    assert pred(_evt_channel_msg("a", "b", "S1-S2")) is True
    assert pred(_evt_channel_msg("a", "b", "S1-S3")) is False


def test_system_and_channel_combined() -> None:
    """REQ 11.3: ``--system`` and ``--channel`` filters combine with AND."""
    pred = _build_tail_predicate(["sender-a"], ["S1-S2"])
    # Match both
    assert pred(_evt_channel_msg("sender-a", "b", "S1-S2")) is True
    # Mismatch system
    assert pred(_evt_channel_msg("other", "b", "S1-S2")) is False
    # Mismatch channel
    assert pred(_evt_channel_msg("sender-a", "b", "S1-S3")) is False


def test_system_filter_matches_sender_or_receiver() -> None:
    """REQ 11.3: system filter matches either sender or receiver in channel events."""
    pred = _build_tail_predicate(["target-b"], [])
    assert pred(_evt_channel_msg("a", "target-b", "S1-S2")) is True
    assert pred(_evt_channel_msg("target-b", "a", "S1-S2")) is True


# ---------------------------------------------------------------------------
# Property: formal predicate semantics over channel_message events
# ---------------------------------------------------------------------------


_systems_strategy = st.lists(
    st.text(min_size=1, max_size=10), min_size=0, max_size=4
)
_channels_strategy = st.lists(
    st.text(min_size=1, max_size=10), min_size=0, max_size=4
)


@settings(max_examples=100)
@given(
    sys_filters=_systems_strategy,
    ch_filters=_channels_strategy,
    sender=st.text(min_size=1, max_size=10),
    receiver=st.text(min_size=1, max_size=10),
    channel=st.text(min_size=1, max_size=10),
)
def test_predicate_property(
    sys_filters: list[str],
    ch_filters: list[str],
    sender: str,
    receiver: str,
    channel: str,
) -> None:
    """Property 12 (REQ 11.3, 11.4): formal predicate semantics.

    ``predicate(e) := (Sys==∅ ∨ system_name(e)∈Sys)
                       ∧ (Ch==∅ ∨ channel_name(e)∈Ch)``.
    """
    evt = _evt_channel_msg(sender, receiver, channel)
    pred = _build_tail_predicate(sys_filters, ch_filters)

    sys_ok = (not sys_filters) or (
        sender in sys_filters or receiver in sys_filters
    )
    ch_ok = (not ch_filters) or (channel in ch_filters)
    expected = sys_ok and ch_ok

    assert pred(evt) == expected
