"""Property 8: S1 reuse vs instantiate dichotomy.

Feature: vsm-poc-platform, Property 8
Validates: Requirements 7.2, 7.3, 13.6

This module implements the **dichotomy half** of Property 8 from
design.md §Correctness Properties. The full Property 8 statement is:

    For any current ``S1Pool`` state ``P`` and any specialization
    request ``s``, define
    ``idle(P, s) := { w ∈ P : w.specialization == s ∧ |w.current_assignments| == 0 }``.
    Then S3_Allocator's allocation step SHALL satisfy:

    * if ``idle(P, s) ≠ ∅``, no ``s1_instantiated`` event is emitted
      for the request and exactly one element of ``idle(P, s)`` receives
      the new assignment via ``S1_S3``;
    * if ``idle(P, s) == ∅`` and ``|P| < 64``, exactly one
      ``s1_instantiated`` event is emitted with ``specialization == s``
      and the newly created S1_Worker receives the initial assignment.

This test focuses on the local pivot of that dichotomy: the
:meth:`vsm.systems.s3_allocator.S1Pool.find_idle` method, which is the
predicate the allocator consults to decide between reuse and
instantiation. The end-to-end allocator behaviour (event emission,
``Platform.spawn_s1`` invocation) is exercised by the integration
scenario tests; here we pin the pure decision function so a regression
in the idle predicate is caught without spinning up the full runtime.

Validates Requirements
----------------------
- REQ 7.2: S3_Allocator SHALL prefer reusing an existing idle S1_Worker
  (specialization match + zero current assignments) over instantiating
  a new one. ``find_idle`` is the implementation of that preference;
  it MUST return a candidate when one exists and ``None`` otherwise.
- REQ 7.3: When no idle S1_Worker is available for a requested
  specialization, the allocator instantiates a new one. ``find_idle``
  returning ``None`` is the precondition that triggers REQ 7.3, so its
  correctness here directly underpins the dichotomy.
- REQ 13.6: The pool ``|P|`` is bounded above by 64. The Hypothesis
  strategy below caps generated pool sizes at 10 to stay well inside
  this bound while still varying the search space meaningfully.

Notes on the testing harness
----------------------------
* The Hypothesis decorator uses ``max_examples=100`` per the
  project-wide convention recorded in tasks.md ("すべての PBT は
  ``@settings(max_examples=100)`` を付与する"). The search space here
  is small and deterministic, so 100 examples is more than sufficient
  to cover the dichotomy branches without runtime concern.
* ``deadline=None`` disables the per-example deadline because the test
  performs no I/O and the assertion logic is trivial — any deadline
  failure would be a CI scheduler artifact, not a real defect.
* :class:`MockS1` and :class:`MockPlatform` are minimal stand-ins that
  expose only the attributes ``find_idle`` actually reads
  (``specialization``, ``current_assignments``, and the
  ``systems[SystemRole.S1_WORKER]`` mapping). This isolates the test
  from the full :class:`vsm.systems.s1_worker.S1_Worker` constructor
  contract, which is irrelevant to the idle predicate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import given, settings, strategies as st

from vsm.roles import SystemRole
from vsm.systems.s3_allocator import S1Pool


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class MockS1:
    """Minimal stand-in for an S1_Worker exposing the two attributes
    that :meth:`S1Pool.find_idle` reads.

    A real :class:`vsm.systems.s1_worker.S1_Worker` carries far more
    state (clock, eventlog, llm, sub-agents, etc.), none of which is
    relevant to the idle predicate. Reducing the surface area to
    ``specialization`` and ``current_assignments`` keeps the property
    test focused and avoids accidentally coupling Property 8 to S1
    construction details.
    """

    system_id: str
    specialization: str
    current_assignments: list = field(default_factory=list)


class MockPlatform:
    """Minimal stand-in for :class:`vsm.runtime.lifecycle.Platform`.

    :class:`S1Pool` only ever reads
    ``platform.systems.get(SystemRole.S1_WORKER, [])`` (see
    ``vsm/systems/s3_allocator.py``), so this mock just exposes that
    one mapping. It deliberately does *not* implement ``spawn_s1`` or
    any other Platform method because Property 8's reuse-vs-instantiate
    dichotomy is decided strictly from the idle predicate, before any
    spawn would be attempted.
    """

    def __init__(self, s1_list: list[MockS1]) -> None:
        self.systems = {SystemRole.S1_WORKER: s1_list}


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# A small, fixed specialization vocabulary keeps the search space
# tractable while still allowing both "match" and "no match" branches
# to be hit by the strategy. The PoC's representative scenario uses
# ``frontend`` and ``test`` (see ``_FALLBACK_SPEC_PLAN`` in
# ``s3_allocator.py``); ``backend`` and ``ops`` are added to broaden
# coverage without inflating the example count.
_SPECS = ["frontend", "backend", "test", "ops"]

# Each S1 instance is generated as a ``(specialization, num_assignments)``
# tuple so the test can construct a :class:`MockS1` whose
# ``current_assignments`` list has the requested length. ``num_assignments``
# is bounded at 3: the only thing that matters for the idle predicate
# is whether the list is empty or non-empty, and 0..3 covers both cases
# with a small overhead. Pool size is capped at 10 (well below REQ 13.6's
# upper bound of 64) to stay focused on the predicate semantics.
_s1_strategy = st.lists(
    st.tuples(st.sampled_from(_SPECS), st.integers(min_value=0, max_value=3)),
    min_size=0,
    max_size=10,
)


# ---------------------------------------------------------------------------
# Property 8 (dichotomy half)
# ---------------------------------------------------------------------------


@given(s1_specs=_s1_strategy, requested_spec=st.sampled_from(_SPECS))
@settings(max_examples=100, deadline=None)
def test_find_idle_returns_matching_idle_or_none(
    s1_specs: list[tuple[str, int]], requested_spec: str
) -> None:
    """Property 8 (dichotomy): ``find_idle`` returns an idle match or ``None``.

    Validates: Requirements 7.2, 7.3, 13.6.

    Strategy
    --------
    For each generated ``(s1_specs, requested_spec)`` pair:

    1. Build a fresh :class:`MockPlatform` whose S1_WORKER list is
       derived from ``s1_specs``. Each tuple ``(spec, n)`` yields a
       :class:`MockS1` with ``specialization=spec`` and
       ``current_assignments`` of length ``n`` (filled with synthetic
       work-item ids that ``find_idle`` ignores — only ``len(...)``
       matters for the idle predicate).
    2. Compute the *expected* set of idle candidates exactly as REQ
       7.2 defines them: ``specialization == requested_spec ∧
       len(current_assignments) == 0``.
    3. Call :meth:`S1Pool.find_idle` and assert the dichotomy:

       * if expected ≠ ∅, the result MUST be a member of the expected
         set with the correct specialization and an empty assignment
         list (REQ 7.2);
       * if expected == ∅, the result MUST be ``None`` so the allocator
         falls through to the REQ 7.3 instantiation branch.
    """
    s1_instances = [
        MockS1(
            system_id=f"s1-{i}",
            specialization=spec,
            current_assignments=[f"w-{j}" for j in range(num_assignments)],
        )
        for i, (spec, num_assignments) in enumerate(s1_specs)
    ]
    platform = MockPlatform(s1_instances)
    pool = S1Pool(platform)

    result = pool.find_idle(requested_spec)

    # REQ 7.2: idle ≡ specialization match AND empty current_assignments.
    idle_candidates = [
        s1
        for s1 in s1_instances
        if s1.specialization == requested_spec and len(s1.current_assignments) == 0
    ]

    if idle_candidates:
        # Branch 1: idle ≠ ∅ — must return one of the candidates with
        # the correct specialization and zero assignments (REQ 7.2).
        assert result is not None, (
            f"find_idle returned None even though idle candidates exist: "
            f"{[s.system_id for s in idle_candidates]}"
        )
        assert result in idle_candidates
        assert result.specialization == requested_spec
        assert len(result.current_assignments) == 0
    else:
        # Branch 2: idle == ∅ — must return None so the caller proceeds
        # to instantiate a new S1 (REQ 7.3).
        assert result is None, (
            f"find_idle returned {result!r} but no idle candidates exist for "
            f"specialization={requested_spec!r} in pool of size {len(s1_instances)}"
        )


# ---------------------------------------------------------------------------
# Deterministic edge cases
# ---------------------------------------------------------------------------


def test_find_idle_returns_first_match() -> None:
    """REQ 7.2: ``find_idle`` returns the first idle match in pool order.

    The S3_Allocator iterates the pool in insertion order (see the
    ``for s1 in s1_list`` loop in ``S1Pool.find_idle``). Pinning the
    "first idle wins" semantics deterministically prevents a future
    refactor from accidentally introducing a non-deterministic
    iteration order (e.g. a set) that would still satisfy the
    Hypothesis property but break the integration scenario which
    relies on stable allocation order for log readability.
    """
    s1_a = MockS1("a", "frontend", [])
    s1_b = MockS1("b", "frontend", [])
    s1_c = MockS1("c", "frontend", ["w1"])  # not idle (non-empty assignments)
    platform = MockPlatform([s1_a, s1_b, s1_c])
    pool = S1Pool(platform)

    result = pool.find_idle("frontend")
    # First idle in pool order is s1_a, even though s1_b is also idle.
    assert result is s1_a


def test_find_idle_no_match() -> None:
    """REQ 7.2 / 7.3: specialization mismatch returns ``None``.

    A pool containing only S1s with non-matching specializations must
    return ``None`` so the allocator triggers the REQ 7.3 instantiation
    branch. This is the canonical "instantiate new" trigger condition.
    """
    s1 = MockS1("a", "test", [])
    platform = MockPlatform([s1])
    pool = S1Pool(platform)
    assert pool.find_idle("frontend") is None


def test_find_idle_empty_pool() -> None:
    """REQ 7.3 / 13.6: an empty pool returns ``None``.

    At the very start of a Run, before any S1 has been instantiated,
    ``find_idle`` MUST return ``None`` so the very first directive
    triggers a fresh ``Platform.spawn_s1`` (REQ 7.3) rather than
    raising on an empty iteration. This also implicitly exercises the
    REQ 13.6 invariant ``0 ≤ |P|`` at the lower boundary.
    """
    platform = MockPlatform([])
    pool = S1Pool(platform)
    assert pool.find_idle("frontend") is None


def test_find_idle_skips_busy_with_matching_spec() -> None:
    """REQ 7.2: an S1 with matching specialization but non-empty
    ``current_assignments`` is NOT considered idle.

    This is the dual of :func:`test_find_idle_no_match`: the pool has
    a specialization match, but the candidate is busy, so the idle
    predicate must reject it and return ``None``. Without this guard
    the allocator would route a new work item to an already-busy S1,
    violating the idle definition in REQ 7.2.
    """
    s1 = MockS1("a", "frontend", ["w1", "w2"])
    platform = MockPlatform([s1])
    pool = S1Pool(platform)
    assert pool.find_idle("frontend") is None
