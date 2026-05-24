"""UTC clock abstraction for the VSM platform.

Provides a `Clock` protocol with two concrete implementations:

* `SystemClock` — production clock backed by `datetime.now(timezone.utc)` and
  `time.monotonic()`.
* `FakeClock` — deterministic test clock whose wall-clock and monotonic time
  advance only when `advance(seconds)` is called.

The `now_iso()` method returns an ISO 8601 timestamp in UTC with millisecond
precision (e.g. ``2025-01-01T00:00:00.000Z``). The format conforms to the
regular expression ``^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{3}Z$``
that is asserted by Task 2.6 / REQ 10.7.

Validates Requirements: 2.8, 2.9, 10.5, 10.7
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "SystemClock", "FakeClock", "format_iso_ms"]


def format_iso_ms(dt: datetime) -> str:
    """Format ``dt`` as ISO 8601 UTC with millisecond precision and ``Z`` suffix.

    The input ``dt`` must be timezone-aware. It is converted to UTC before
    formatting so the trailing ``Z`` is always accurate.
    """
    if dt.tzinfo is None:
        raise ValueError("format_iso_ms requires a timezone-aware datetime")
    utc = dt.astimezone(timezone.utc)
    millis = utc.microsecond // 1000
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"


@runtime_checkable
class Clock(Protocol):
    """Protocol describing the clock surface used by the VSM platform."""

    def now(self) -> datetime:
        """Return the current UTC wall-clock time as a timezone-aware datetime."""
        ...

    def now_iso(self) -> str:
        """Return the current UTC wall-clock time as an ISO 8601 / ms string."""
        ...

    def monotonic(self) -> float:
        """Return a monotonically non-decreasing seconds value (for SLA timing)."""
        ...


class SystemClock:
    """Production clock backed by the operating system clocks."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_iso(self) -> str:
        return format_iso_ms(self.now())

    def monotonic(self) -> float:
        return time.monotonic()


_DEFAULT_FAKE_EPOCH = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    """Deterministic clock for tests.

    The wall-clock and monotonic readings are frozen until `advance(seconds)`
    is called. `monotonic()` starts at 0 and advances by exactly the amount
    passed to `advance()`; `now()` advances by the same delta from the
    initial datetime.
    """

    def __init__(self, initial: datetime | None = None) -> None:
        if initial is None:
            initial = _DEFAULT_FAKE_EPOCH
        if initial.tzinfo is None:
            raise ValueError("FakeClock initial datetime must be timezone-aware")
        # Store initial state in UTC for consistent now_iso output.
        self._initial: datetime = initial.astimezone(timezone.utc)
        self._elapsed: float = 0.0

    def advance(self, seconds: float) -> None:
        """Advance both the wall and monotonic clocks by ``seconds``.

        Negative values are rejected to preserve monotonicity.
        """
        if seconds < 0:
            raise ValueError("FakeClock.advance does not accept negative seconds")
        self._elapsed += float(seconds)

    def now(self) -> datetime:
        return self._initial + timedelta(seconds=self._elapsed)

    def now_iso(self) -> str:
        return format_iso_ms(self.now())

    def monotonic(self) -> float:
        return self._elapsed
