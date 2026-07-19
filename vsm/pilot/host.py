from __future__ import annotations

from datetime import datetime

from vsm.errors import InvariantViolation
from vsm.kernel.models import ExecutionState
from vsm.kernel.service import Kernel
from vsm.pilot.models import (
    DeviceIdentity,
    PilotHostState,
    PilotHostStatus,
)


class PilotHostCoordinator:
    """Tracks outbound PilotHost streams, cursor acknowledgements, and pause."""

    def __init__(self, kernel: Kernel, *, expected_identity: DeviceIdentity) -> None:
        self.kernel = kernel
        self.expected_identity = expected_identity
        self.hosts: dict[str, PilotHostStatus] = {}

    def _validate_cursor(self, cursor: int) -> None:
        if cursor == 0:
            return
        page = self.kernel.ledger.page(cursor - 1, 1)
        if not page or page[0].cursor != cursor:
            raise InvariantViolation(
                "PilotHost acknowledged cursor does not exist in this DataSpace"
            )

    def connect(
        self,
        *,
        identity: DeviceIdentity,
        acknowledged_cursor: int,
        connected_at: datetime,
    ) -> PilotHostStatus:
        if identity != self.expected_identity:
            raise InvariantViolation("PilotHost device identity is not registered")
        self._validate_cursor(acknowledged_cursor)
        previous = self.hosts.get(identity.pilot_host_id)
        if previous is not None:
            if previous.identity != identity:
                raise InvariantViolation("PilotHost device identity changed")
            if acknowledged_cursor < previous.acknowledged_cursor:
                raise InvariantViolation("PilotHost cursor acknowledgement regressed")
        status = PilotHostStatus(
            identity=identity,
            state=PilotHostState.CONNECTED,
            acknowledged_cursor=acknowledged_cursor,
            connected_at=connected_at,
            disconnected_at=None,
        )
        self.hosts[identity.pilot_host_id] = status
        return status

    def acknowledge(self, pilot_host_id: str, cursor: int) -> PilotHostStatus:
        host = self.hosts.get(pilot_host_id)
        if host is None or host.state is not PilotHostState.CONNECTED:
            raise InvariantViolation("PilotHost is not connected")
        if cursor < host.acknowledged_cursor:
            raise InvariantViolation("PilotHost cursor acknowledgement regressed")
        self._validate_cursor(cursor)
        updated = host.model_copy(update={"acknowledged_cursor": cursor})
        self.hosts[pilot_host_id] = updated
        return updated

    def disconnect(
        self, pilot_host_id: str, *, disconnected_at: datetime, idempotency_key: str
    ) -> PilotHostStatus:
        host = self.hosts.get(pilot_host_id)
        if host is None or host.state is not PilotHostState.CONNECTED:
            raise InvariantViolation("PilotHost is not connected")
        self.kernel.pilot_host_disconnected(
            pilot_host_id, idempotency_key=idempotency_key
        )
        updated = host.model_copy(
            update={
                "state": PilotHostState.DISCONNECTED,
                "disconnected_at": disconnected_at,
            }
        )
        self.hosts[pilot_host_id] = updated
        return updated

    def resume_execution(
        self,
        execution_id: str,
        *,
        pilot_host_id: str,
        actor_id: str,
        idempotency_key: str,
    ) -> None:
        host = self.hosts.get(pilot_host_id)
        if host is None or host.state is not PilotHostState.CONNECTED:
            raise InvariantViolation("PilotHost must reconnect before Execution resumes")
        execution = self.kernel.executions.get(execution_id)
        if execution is None or execution.pilot_host_id != pilot_host_id:
            raise InvariantViolation("Execution does not belong to PilotHost")
        self.kernel.set_execution_state(
            execution_id,
            ExecutionState.ACTIVE,
            actor_type="pilot",
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            pause_reason=None,
        )
