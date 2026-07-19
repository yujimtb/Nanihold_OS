from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field

from vsm.activation.models import ActivationState
from vsm.errors import InvariantViolation
from vsm.ids import deterministic_event_id, new_id
from vsm.kernel.ledger import OperationalLedger
from vsm.kernel.models import EventEnvelope, Identifier, NonBlank, StrictModel

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BootstrapCodeRecord(StrictModel):
    bootstrap_id: Identifier
    owner_id: Identifier
    code_sha256: Sha256
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None


class BrowserSessionRecord(StrictModel):
    session_id: Identifier
    owner_id: Identifier
    device_id: NonBlank
    token_sha256: Sha256
    issued_at: datetime
    expires_at: datetime
    state: Literal["active", "revoked"]


class OwnerBootstrapGrant(StrictModel):
    bootstrap_id: Identifier
    code: NonBlank
    link: NonBlank
    expires_at: datetime


class OwnerSessionGrant(StrictModel):
    session_token: NonBlank
    device_id: NonBlank
    expires_at: datetime


class OwnerBootstrapService:
    """One-time owner bootstrap; only token hashes enter the Event Ledger."""

    def __init__(
        self,
        *,
        data_space_id: str,
        owner_id: str,
        ledger: OperationalLedger,
        clock: Callable[[], datetime],
        activation_state: Callable[[], ActivationState],
        owner_node_exists: Callable[[], bool],
    ) -> None:
        self.data_space_id = data_space_id
        self.owner_id = owner_id
        self.ledger = ledger
        self.clock = clock
        self.activation_state = activation_state
        self.owner_node_exists = owner_node_exists
        self.codes: dict[str, BootstrapCodeRecord] = {}
        self.sessions: dict[str, BrowserSessionRecord] = {}
        self._stream_id = f"owner-auth:{data_space_id.split(':', 1)[-1]}"
        self._version = 0

    @staticmethod
    def _sha(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _commissioned(self) -> None:
        if (
            self.activation_state() is ActivationState.UNCOMMISSIONED
            or not self.owner_node_exists()
        ):
            raise InvariantViolation("owner bootstrap requires commissioned history and owner node")

    def _record(
        self, event_type: str, payload: dict[str, object], idempotency_key: str
    ) -> None:
        event = EventEnvelope(
            event_id=deterministic_event_id(
                data_space_id=self.data_space_id,
                stream_id=self._stream_id,
                idempotency_key=idempotency_key,
            ),
            data_space_id=self.data_space_id,
            stream_id=self._stream_id,
            stream_version=self._version + 1,
            event_type=event_type,
            occurred_at=self.clock(),
            actor_type="system",
            actor_id=None,
            correlation_id=self._stream_id,
            causation_id=None,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        result = self.ledger.append(event, self._version)
        self._version = result.stream_version

    def issue(
        self,
        *,
        base_url: str,
        lifetime_seconds: int,
        idempotency_key: str,
    ) -> OwnerBootstrapGrant:
        self._commissioned()
        if lifetime_seconds <= 0 or lifetime_seconds > 900:
            raise InvariantViolation("bootstrap lifetime must be 1..900 seconds")
        code = secrets.token_urlsafe(32)
        now = self.clock()
        record = BootstrapCodeRecord(
            bootstrap_id=new_id("bootstrap"),
            owner_id=self.owner_id,
            code_sha256=self._sha(code),
            issued_at=now,
            expires_at=now + timedelta(seconds=lifetime_seconds),
            used_at=None,
        )
        self._record(
            "owner_bootstrap_issued",
            {"record": record.model_dump(mode="json")},
            idempotency_key,
        )
        self.codes[record.bootstrap_id] = record
        return OwnerBootstrapGrant(
            bootstrap_id=record.bootstrap_id,
            code=code,
            link=f"{base_url.rstrip('/')}/owner-bootstrap?code={code}",
            expires_at=record.expires_at,
        )

    def exchange(
        self,
        *,
        code: str,
        device_id: str,
        session_lifetime_seconds: int,
        idempotency_key: str,
    ) -> OwnerSessionGrant:
        self._commissioned()
        if not device_id.strip():
            raise InvariantViolation("owner browser device_id is required")
        if session_lifetime_seconds <= 0:
            raise InvariantViolation("owner session lifetime must be positive")
        digest = self._sha(code)
        record = next(
            (item for item in self.codes.values() if item.code_sha256 == digest),
            None,
        )
        now = self.clock()
        if record is None or record.owner_id != self.owner_id:
            raise InvariantViolation("owner bootstrap code is invalid")
        if record.used_at is not None:
            raise InvariantViolation("owner bootstrap code was already used")
        if record.expires_at <= now:
            raise InvariantViolation("owner bootstrap code expired")
        token = secrets.token_urlsafe(48)
        session = BrowserSessionRecord(
            session_id=new_id("owner-session"),
            owner_id=self.owner_id,
            device_id=device_id,
            token_sha256=self._sha(token),
            issued_at=now,
            expires_at=now + timedelta(seconds=session_lifetime_seconds),
            state="active",
        )
        used = record.model_copy(update={"used_at": now})
        self._record(
            "owner_bootstrap_exchanged",
            {
                "bootstrap_id": record.bootstrap_id,
                "used_at": now.isoformat(),
                "session": session.model_dump(mode="json"),
            },
            idempotency_key,
        )
        self.codes[record.bootstrap_id] = used
        self.sessions[session.session_id] = session
        return OwnerSessionGrant(
            session_token=token,
            device_id=device_id,
            expires_at=session.expires_at,
        )

    def authenticate(self, token: str) -> str:
        digest = self._sha(token)
        session = next(
            (item for item in self.sessions.values() if item.token_sha256 == digest),
            None,
        )
        if (
            session is None
            or session.owner_id != self.owner_id
            or session.state != "active"
            or session.expires_at <= self.clock()
        ):
            raise InvariantViolation("owner browser session is invalid or expired")
        return session.device_id
