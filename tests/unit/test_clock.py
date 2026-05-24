"""Unit tests for `vsm.clock`.

Validates Requirements: 10.7

REQ 10.7 — every appended Event_Log entry must carry a UTC timestamp in ISO
8601 format with millisecond precision. The clock abstraction in
`vsm.clock` is the single source of those timestamps, so these tests pin the
formatting (regex `^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$`),
the timezone awareness of `now()`, the monotonic non-decrease guarantee on
`SystemClock`, and the deterministic semantics of `FakeClock` used by the
rest of the test suite.

Plain `pytest` only — no Hypothesis usage.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from vsm.clock import Clock, FakeClock, SystemClock, format_iso_ms

# Pinned by REQ 10.7. Kept as a module constant so every test asserts the same
# format and a future drift is caught in one place.
ISO_MS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


# ---------------------------------------------------------------------------
# SystemClock
# ---------------------------------------------------------------------------


def test_system_clock_now_iso_matches_regex() -> None:
    """REQ 10.7: `now_iso()` matches the ms-precision ISO 8601 UTC regex."""
    iso = SystemClock().now_iso()
    assert ISO_MS_RE.match(iso), f"unexpected timestamp format: {iso!r}"


def test_system_clock_now_iso_regex_repeated_samples() -> None:
    """The format must hold across multiple samples, not just one lucky call."""
    clock = SystemClock()
    for _ in range(10):
        iso = clock.now_iso()
        assert ISO_MS_RE.match(iso), f"unexpected timestamp format: {iso!r}"


def test_system_clock_now_returns_timezone_aware_utc() -> None:
    now = SystemClock().now()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None, "SystemClock.now() must be timezone-aware"
    # UTC offset is exactly zero.
    assert now.utcoffset() == timedelta(0)


def test_system_clock_monotonic_returns_float() -> None:
    value = SystemClock().monotonic()
    assert isinstance(value, float)


def test_system_clock_monotonic_is_non_decreasing() -> None:
    """`SystemClock.monotonic()` must never go backward across successive calls."""
    clock = SystemClock()
    samples = [clock.monotonic() for _ in range(20)]
    for earlier, later in zip(samples, samples[1:]):
        assert later >= earlier, (
            f"monotonic() decreased: {earlier} -> {later} in {samples!r}"
        )


def test_system_clock_satisfies_clock_protocol() -> None:
    """`SystemClock` must be usable wherever a `Clock` is expected."""
    assert isinstance(SystemClock(), Clock)


# ---------------------------------------------------------------------------
# FakeClock
# ---------------------------------------------------------------------------


def test_fake_clock_initial_monotonic_is_zero() -> None:
    assert FakeClock().monotonic() == 0.0


def test_fake_clock_advance_updates_monotonic() -> None:
    clock = FakeClock()
    clock.advance(5.0)
    assert clock.monotonic() == 5.0


def test_fake_clock_advance_is_cumulative() -> None:
    clock = FakeClock()
    clock.advance(1.5)
    clock.advance(2.5)
    assert clock.monotonic() == 4.0


def test_fake_clock_advance_zero_is_allowed() -> None:
    """Zero-second advances are a valid no-op (monotonicity is preserved)."""
    clock = FakeClock()
    clock.advance(0.0)
    assert clock.monotonic() == 0.0


def test_fake_clock_advance_negative_raises() -> None:
    with pytest.raises(ValueError):
        FakeClock().advance(-1.0)


def test_fake_clock_default_initial_now_iso_format() -> None:
    """Even with the default epoch, `now_iso()` must satisfy REQ 10.7."""
    iso = FakeClock().now_iso()
    assert ISO_MS_RE.match(iso), f"unexpected timestamp format: {iso!r}"


def test_fake_clock_explicit_initial_now_iso_value() -> None:
    initial = datetime(2025, 1, 1, tzinfo=timezone.utc)
    clock = FakeClock(initial=initial)
    assert clock.now_iso() == "2025-01-01T00:00:00.000Z"


def test_fake_clock_advance_moves_now_iso() -> None:
    initial = datetime(2025, 1, 1, tzinfo=timezone.utc)
    clock = FakeClock(initial=initial)
    clock.advance(1.5)  # 1500 ms
    assert clock.now_iso() == "2025-01-01T00:00:01.500Z"


def test_fake_clock_naive_initial_raises() -> None:
    with pytest.raises(ValueError):
        FakeClock(initial=datetime(2025, 1, 1))  # naive — no tzinfo


def test_fake_clock_now_is_timezone_aware_utc() -> None:
    now = FakeClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_fake_clock_satisfies_clock_protocol() -> None:
    assert isinstance(FakeClock(), Clock)


# ---------------------------------------------------------------------------
# format_iso_ms
# ---------------------------------------------------------------------------


def test_format_iso_ms_matches_regex_for_utc_input() -> None:
    dt = datetime(2025, 6, 15, 12, 34, 56, 789000, tzinfo=timezone.utc)
    result = format_iso_ms(dt)
    assert result == "2025-06-15T12:34:56.789Z"
    assert ISO_MS_RE.match(result)


def test_format_iso_ms_truncates_sub_millisecond_microseconds() -> None:
    """Microseconds must be truncated (not rounded) to milliseconds."""
    dt = datetime(2025, 1, 1, 0, 0, 0, 999_999, tzinfo=timezone.utc)
    assert format_iso_ms(dt) == "2025-01-01T00:00:00.999Z"


def test_format_iso_ms_zero_microseconds_pads_to_three_digits() -> None:
    dt = datetime(2025, 1, 1, 0, 0, 0, 0, tzinfo=timezone.utc)
    assert format_iso_ms(dt) == "2025-01-01T00:00:00.000Z"


def test_format_iso_ms_converts_non_utc_to_utc() -> None:
    """Non-UTC tz-aware inputs must be normalized to UTC before formatting."""
    plus_nine = timezone(timedelta(hours=9))
    dt = datetime(2025, 1, 1, 9, 0, 0, tzinfo=plus_nine)  # == 2025-01-01T00:00:00Z
    assert format_iso_ms(dt) == "2025-01-01T00:00:00.000Z"


def test_format_iso_ms_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        format_iso_ms(datetime(2025, 1, 1))  # naive — no tzinfo
