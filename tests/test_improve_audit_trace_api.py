from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace

import httpx
import pytest

from test_web_api import auth, client as make_client
from vsm.agent_naming import AgentNameAssignment
from vsm.audit_trace import AuditTraceService, _AuditTraceCancelled
from vsm.errors import InvariantViolation
from vsm.kernel.models import (
    EventEnvelope,
    Execution,
    ExecutionState,
    StoredEvent,
    WorkItem,
    WorkState,
)
from vsm.notifications import (
    AgentNotification,
    NotificationPlatform,
    NotificationResolutionKind,
    NotificationSourceKind,
)


class InstrumentedLedger:
    def __init__(
        self,
        events: list[StoredEvent],
        streams: dict[str, list[StoredEvent]],
    ) -> None:
        self.events = events
        self.streams = streams
        self.page_calls = 0
        self.stream_calls: list[tuple[str, int, int]] = []

    def page(self, after_cursor: int, limit: int) -> list[StoredEvent]:
        self.page_calls += 1
        return self.events[after_cursor : after_cursor + limit]

    def stream(
        self, stream_id: str, after_stream_version: int, limit: int
    ) -> list[StoredEvent]:
        self.stream_calls.append((stream_id, after_stream_version, limit))
        return [
            stored
            for stored in self.streams.get(stream_id, [])
            if stored.event.stream_version > after_stream_version
        ][:limit]


def _stored(
    *,
    cursor: int,
    stream_id: str,
    stream_version: int,
    event_type: str,
    payload: dict[str, object],
) -> StoredEvent:
    event = EventEnvelope(
        event_id=f"event:audit-trace-{cursor}",
        data_space_id="space:personal",
        stream_id=stream_id,
        stream_version=stream_version,
        event_type=event_type,
        occurred_at="2026-07-23T00:00:00Z",
        actor_type="system",
        actor_id="system:audit-test",
        correlation_id=None,
        causation_id=None,
        idempotency_key=f"audit-trace-test:{cursor}",
        payload=payload,
    )
    return StoredEvent(cursor=cursor, event=event)


def _fixture_with_fifty_thousand_events() -> tuple[SimpleNamespace, InstrumentedLedger]:
    work_item = WorkItem(
        work_item_id="work:audit-performance",
        data_space_id="space:personal",
        title="Audit performance",
        description="A synthetic execution for the audit trace performance test.",
        owner_node_id="node:owner",
        delegated_to_node_id="node:worker",
        integration_owner_node_id="node:owner",
        parent_work_item_id=None,
        acceptance_criteria=("The trace is indexed by stream.",),
        route_key="route:audit-performance",
        state=WorkState.READY,
        blocking_s3_star_finding_ids=(),
        completion_evidence=None,
    )
    execution = Execution(
        execution_id="execution:audit-performance",
        data_space_id="space:personal",
        node_id="node:worker",
        work_item_id=work_item.work_item_id,
        pilot_id="pilot:audit-performance",
        model_candidate_key="candidate:audit-performance",
        state=ExecutionState.SUCCEEDED,
        provider_session_id="session:audit-performance",
        pilot_host_id="pilot-host:audit-performance",
        pause_reason=None,
        agent_name="Toki",
    )
    assignment = AgentNameAssignment(
        assignment_id="assignment:audit-performance",
        data_space_id="space:personal",
        work_item_id=work_item.work_item_id,
        execution_id=execution.execution_id,
        node_id=execution.node_id,
        pilot_id=execution.pilot_id,
        agent_name="Toki",
        base_name="Toki",
        suffix=1,
        name_column="英",
        scale=1,
        provider="test",
        model_candidate_key=execution.model_candidate_key,
    )
    notification = AgentNotification(
        notification_id="notification:audit-performance",
        data_space_id="space:personal",
        source_kind=NotificationSourceKind.CHANNEL_INBOUND,
        source_platform=NotificationPlatform.SLACK,
        source_instance_id="device:audit-test",
        source_channel="C123",
        source_message_id="message:audit-performance",
        source_observation_subject="message:slack:audit-performance",
        sender_actor_id="actor:audit-test",
        recipient_agent_name="Toki",
        body="@Toki inspect the audit trace",
        resolution_kind=NotificationResolutionKind.EXPLICIT_MENTION,
        requires_work_item=False,
    )

    assignment_event = _stored(
        cursor=1,
        stream_id=assignment.assignment_id,
        stream_version=1,
        event_type="agent_name_assigned",
        payload={"assignment": assignment.model_dump(mode="json")},
    )
    noise = [
        _stored(
            cursor=cursor,
            stream_id=f"noise:{cursor}",
            stream_version=1,
            event_type="synthetic_noise",
            payload={},
        )
        for cursor in range(2, 50_002)
    ]
    receipt_event = _stored(
        cursor=50_002,
        stream_id=execution.execution_id,
        stream_version=1,
        event_type="pilot_execution_receipt_recorded",
        payload={
            "receipt_id": "receipt:audit-performance",
            "receipt_status": "succeeded",
            "agent_name": "Toki",
            "work_item_id": work_item.work_item_id,
            "execution_id": execution.execution_id,
            "provider_session_id": execution.provider_session_id,
        },
    )
    delivery_event = _stored(
        cursor=50_003,
        stream_id=notification.notification_id,
        stream_version=1,
        event_type="agent_notification_delivered",
        payload={"notification": notification.model_dump(mode="json")},
    )
    events = [assignment_event, *noise, receipt_event, delivery_event]
    ledger = InstrumentedLedger(
        events,
        {
            assignment.assignment_id: [assignment_event],
            execution.execution_id: [receipt_event],
            notification.notification_id: [delivery_event],
        },
    )
    kernel = SimpleNamespace(
        ledger=ledger,
        work_items={work_item.work_item_id: work_item},
        executions={execution.execution_id: execution},
        agent_name_assignments={assignment.assignment_id: assignment},
        agent_notifications={notification.notification_id: notification},
    )
    return kernel, ledger


def test_stream_trace_matches_canonical_and_scales_to_fifty_thousand_events():
    kernel, ledger = _fixture_with_fifty_thousand_events()
    service = AuditTraceService(kernel)

    started = time.perf_counter()
    optimized_execution = service.trace_execution("execution:audit-performance")
    optimized_notification = service.trace_notification("notification:audit-performance")
    elapsed_seconds = time.perf_counter() - started

    print(f"ATA-03 synthetic 50,003-event trace pair: {elapsed_seconds:.4f}s")
    assert elapsed_seconds < 5
    assert ledger.page_calls == 0
    assert {call[0] for call in ledger.stream_calls} == {
        "assignment:audit-performance",
        "execution:audit-performance",
        "notification:audit-performance",
    }

    canonical_execution = service._trace_execution_canonical(
        "execution:audit-performance"
    )
    canonical_notification = service._trace_notification_canonical(
        "notification:audit-performance"
    )

    assert optimized_execution == canonical_execution
    assert optimized_notification == canonical_notification
    assert ledger.page_calls > 0


def test_stream_trace_preserves_invariant_failure_from_canonical():
    kernel, ledger = _fixture_with_fifty_thousand_events()
    ledger.streams["execution:audit-performance"] = []
    ledger.events = [
        *ledger.events[:50_001],
        ledger.events[-1].model_copy(update={"cursor": 50_002}),
    ]
    service = AuditTraceService(kernel)

    with pytest.raises(InvariantViolation) as optimized_error:
        service.trace_execution("execution:audit-performance")
    with pytest.raises(InvariantViolation) as canonical_error:
        service._trace_execution_canonical("execution:audit-performance")

    assert type(optimized_error.value) is type(canonical_error.value)
    assert str(optimized_error.value) == str(canonical_error.value)


@pytest.mark.asyncio
async def test_audit_routes_are_async_and_return_busy_when_configured_capacity_is_full(
    system, monkeypatch
):
    test_client = make_client(system)
    started = Event()
    release = Event()
    count_lock = Lock()
    started_count = 0

    class BlockingTrace:
        def __init__(self, _kernel, *, cancellation_event):
            self.cancellation_event = cancellation_event

        def trace_notification(self, notification_id):
            nonlocal started_count
            with count_lock:
                started_count += 1
                if started_count == 2:
                    started.set()
            if not release.wait(3):
                raise AssertionError("test trace was not released")
            return {"notification_id": notification_id, "verified": True}

    monkeypatch.setattr("vsm.web.app.AuditTraceService", BlockingTrace)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=test_client.app),
            base_url="https://testserver",
        ) as api:
            first = asyncio.create_task(
                api.get("/api/audit-traces/notifications/notification:first", headers=auth())
            )
            second = asyncio.create_task(
                api.get("/api/audit-traces/notifications/notification:second", headers=auth())
            )
            assert await asyncio.to_thread(started.wait, 2)

            busy = await api.get(
                "/api/audit-traces/notifications/notification:busy", headers=auth()
            )
            assert busy.status_code == 503
            assert busy.headers["retry-after"] == "1"

            release.set()
            first_response, second_response = await asyncio.gather(first, second)
            assert first_response.status_code == 200
            assert second_response.status_code == 200
            assert first_response.json()["verified"] is True
            assert second_response.json()["verified"] is True
    finally:
        test_client.close()


def test_cancellation_stops_before_a_new_stream_request():
    kernel, _ = _fixture_with_fifty_thousand_events()

    class BlockingStreamLedger(InstrumentedLedger):
        def __init__(self, delegate: InstrumentedLedger) -> None:
            super().__init__(delegate.events, delegate.streams)
            self.first_stream_started = Event()
            self.release = Event()

        def stream(
            self, stream_id: str, after_stream_version: int, limit: int
        ) -> list[StoredEvent]:
            self.first_stream_started.set()
            self.release.wait(2)
            return super().stream(stream_id, after_stream_version, limit)

    blocking_ledger = BlockingStreamLedger(kernel.ledger)
    kernel.ledger = blocking_ledger
    cancellation_event = Event()
    service = AuditTraceService(kernel, cancellation_event=cancellation_event)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.trace_execution, "execution:audit-performance"
        )
        assert blocking_ledger.first_stream_started.wait(2)
        cancellation_event.set()
        blocking_ledger.release.set()
        with pytest.raises(_AuditTraceCancelled):
            future.result(timeout=2)

    assert len(blocking_ledger.stream_calls) == 1
