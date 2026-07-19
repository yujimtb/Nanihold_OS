from __future__ import annotations

import re
import uuid
import hashlib
import json

from vsm.errors import InvariantViolation

_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9._~-]{1,160}$")


def new_id(kind: str) -> str:
    value = f"{kind}:{uuid.uuid4()}"
    validate_id(value)
    return value


def deterministic_event_id(
    *, data_space_id: str, stream_id: str, idempotency_key: str
) -> str:
    canonical = json.dumps(
        {
            "data_space_id": data_space_id,
            "stream_id": stream_id,
            "idempotency_key": idempotency_key,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    value = f"event:{hashlib.sha256(canonical).hexdigest()}"
    validate_id(value)
    return value


def validate_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise InvariantViolation(f"invalid Nanihold identifier: {value!r}")
    return value
