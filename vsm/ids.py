from __future__ import annotations

import re
import uuid

from vsm.errors import InvariantViolation

_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9._~-]{1,160}$")


def new_id(kind: str) -> str:
    value = f"{kind}:{uuid.uuid4()}"
    validate_id(value)
    return value


def validate_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise InvariantViolation(f"invalid Nanihold identifier: {value!r}")
    return value
