"""Property 9 (Conflict detection correctness). Validates Requirements: 8.2."""
from __future__ import annotations

from collections import defaultdict

from hypothesis import given, settings, strategies as st

from vsm.systems.s2_coordinator import detect_conflict, Conflict


def test_empty_input():
    """Empty input → empty list."""
    assert detect_conflict({}) == []


def test_single_s1_no_conflict():
    """Single S1 with assignments → no conflict (need ≥ 2 S1s sharing a wi)."""
    states = {
        "s1-a": {"specialization": "x", "current_assignments": ["wi-1", "wi-2"]},
    }
    assert detect_conflict(states) == []


def test_two_s1s_same_spec_same_wi():
    """Two S1s with same spec + same work_item_id → one conflict."""
    states = {
        "s1-a": {"specialization": "x", "current_assignments": ["wi-1"]},
        "s1-b": {"specialization": "x", "current_assignments": ["wi-1"]},
    }
    conflicts = detect_conflict(states)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.specialization == "x"
    assert c.work_item_id == "wi-1"
    assert set(c.s1_ids) == {"s1-a", "s1-b"}


def test_two_s1s_same_spec_different_wi():
    """Two S1s with same spec but different work_item_ids → no conflict."""
    states = {
        "s1-a": {"specialization": "x", "current_assignments": ["wi-1"]},
        "s1-b": {"specialization": "x", "current_assignments": ["wi-2"]},
    }
    assert detect_conflict(states) == []


def test_two_s1s_different_spec_same_wi():
    """Two S1s with different specs sharing work_item_id → no conflict.

    REQ 8.2 keys on the (specialization, work_item_id) pair, so different
    specializations holding the same wi are not a conflict.
    """
    states = {
        "s1-a": {"specialization": "x", "current_assignments": ["wi-1"]},
        "s1-b": {"specialization": "y", "current_assignments": ["wi-1"]},
    }
    assert detect_conflict(states) == []


def test_three_s1s_same_spec_same_wi():
    """Three S1s with same spec sharing same work_item → one conflict with all 3 ids."""
    states = {
        "s1-a": {"specialization": "x", "current_assignments": ["wi-1"]},
        "s1-b": {"specialization": "x", "current_assignments": ["wi-1"]},
        "s1-c": {"specialization": "x", "current_assignments": ["wi-1"]},
    }
    conflicts = detect_conflict(states)
    assert len(conflicts) == 1
    assert conflicts[0].specialization == "x"
    assert conflicts[0].work_item_id == "wi-1"
    assert set(conflicts[0].s1_ids) == {"s1-a", "s1-b", "s1-c"}


# ----------------------------------------------------------------------
# Strategies for Property 9
# ----------------------------------------------------------------------
# Specializations and work-item IDs are drawn from small fixed pools so
# that collisions (and therefore conflicts) occur with non-trivial
# probability. Pure-random strings would almost never collide and would
# leave the conflict branch of ``detect_conflict`` untested.
_specs = st.sampled_from(["frontend", "backend", "test"])
_wi_ids = st.sampled_from(["wi-1", "wi-2", "wi-3", "wi-4"])
# ``unique=True`` mirrors the real S1Worker contract: a single S1 holds
# each work item at most once in ``current_assignments``.
_assignments = st.lists(_wi_ids, min_size=0, max_size=3, unique=True)


@given(s1_states=st.dictionaries(
    keys=st.text(min_size=1, max_size=5),
    values=st.fixed_dictionaries({
        "specialization": _specs,
        "current_assignments": _assignments,
    }),
    min_size=0, max_size=10,
))
@settings(max_examples=100, deadline=None)
def test_property_9_conflict_detection_correctness(s1_states):
    """Property 9 (REQ 8.2): detect_conflict matches expected formula.

    expected(S) := { (spec, wi) | |{ s ∈ S :
        s.specialization == spec ∧ wi ∈ s.current_assignments }| ≥ 2 }

    **Validates: Requirements 8.2**
    """
    # Compute expected via reference implementation of REQ 8.2.
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for s1_id, state in s1_states.items():
        spec = state["specialization"]
        for wi in state["current_assignments"]:
            groups[(spec, wi)].add(s1_id)
    expected_conflicts = {
        (spec, wi): s1_ids for (spec, wi), s1_ids in groups.items()
        if len(s1_ids) >= 2
    }

    conflicts = detect_conflict(s1_states)

    # Projection (specialization, work_item_id) of detect_conflict equals
    # the keys of expected(S).
    actual_keys = {(c.specialization, c.work_item_id) for c in conflicts}
    assert actual_keys == set(expected_conflicts.keys())

    # For each returned Conflict, s1_ids contains every S1 holding that
    # (spec, wi) pair, and the REQ 8.2 invariant |s1_ids| >= 2 holds.
    for c in conflicts:
        assert isinstance(c, Conflict)
        assert set(c.s1_ids) == expected_conflicts[(c.specialization, c.work_item_id)]
        assert len(c.s1_ids) >= 2
