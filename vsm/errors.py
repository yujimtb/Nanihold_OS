class NaniholdError(Exception):
    """Base error for explicit Nanihold failures."""


class ConfigurationError(NaniholdError):
    """Required configuration is absent or inconsistent."""


class InvariantViolation(NaniholdError):
    """A Kernel invariant would be violated."""


class StreamConflict(NaniholdError):
    def __init__(self, stream_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"stream {stream_id!r} expected version {expected}, actual version {actual}"
        )
        self.stream_id = stream_id
        self.expected = expected
        self.actual = actual


class ModelMismatch(NaniholdError):
    """A provider answered with a model other than the requested model."""


class ReconciliationRequired(NaniholdError):
    """An effect result is unknown and must be reconciled by idempotency key."""
