from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import Future, wait
from threading import RLock
from typing import Protocol

from pydantic import Field

from vsm.agent_naming import AgentNameRegistry
from vsm.errors import InvariantViolation
from vsm.environment_instance import EnvironmentInstance
from vsm.ids import new_id
from vsm.kernel.models import (
    Execution,
    ExecutionState,
    Identifier,
    NonBlank,
    RouteSnapshot,
    RouteSnapshotState,
    StrictModel,
    WorkEdgeKind,
    WorkItem,
    WorkState,
)
from vsm.kernel.service import Kernel
from vsm.pilot.models import EventDeltaSummary, ModelCandidate
from vsm.pilot.production_host import (
    ArtifactReference,
    PilotHostModelMismatch,
    PilotHostReceipt,
    PilotHostReceiptError,
    PilotHostTransportUnknown,
    PilotHostUnreachable,
    WorkExecutionOutcome,
)
from vsm.routing.bayesian import BayesianRouter


logger = logging.getLogger(__name__)


class PilotBinding(StrictModel):
    node_id: Identifier
    pilot_id: Identifier
    pilot_host_id: Identifier


class DispatchAssignment(StrictModel):
    work_item_id: Identifier
    execution_id: Identifier
    pilot_id: Identifier
    pilot_host_id: Identifier
    model_candidate_key: NonBlank
    agent_name: NonBlank | None = None


class DispatchBatch(StrictModel):
    assignments: tuple[DispatchAssignment, ...]
    parallelism: int = Field(ge=0)
    model_calls: int = Field(ge=0)


class WorkExecutor(Protocol):
    def validate_work_candidate(self, candidate: ModelCandidate) -> None: ...

    def execute_work(
        self,
        *,
        execution_id: str,
        work_item: WorkItem,
        candidate: ModelCandidate,
        event_delta: EventDeltaSummary,
        artifact_refs: tuple[ArtifactReference, ...],
        idempotency_key: str,
        agent_name: str,
    ) -> WorkExecutionOutcome: ...


class EnvironmentFailover(Protocol):
    """Injected environment failover boundary used by the dispatcher."""

    def failover(
        self, failed_instance_id: str, *, idempotency_key: str
    ) -> EnvironmentInstance | None: ...


class DependencyAwareDispatcher:
    """Dispatches independent ready work concurrently through the typed host."""

    def __init__(
        self,
        *,
        kernel: Kernel,
        router: BayesianRouter,
        evidence_cursor: Callable[[], int],
        startup_projection_cursor: int,
        model_registry: dict[str, ModelCandidate] | None = None,
        work_executor: WorkExecutor | None = None,
        agent_naming_registry: AgentNameRegistry | None = None,
        environment_failover: EnvironmentFailover | None = None,
        max_parallelism: int | None = None,
    ) -> None:
        self.kernel = kernel
        self.router = router
        self.evidence_cursor = evidence_cursor
        if startup_projection_cursor < 0:
            raise InvariantViolation(
                "startup Projection cursor must be non-negative"
            )
        # Projection has already verified every event through this cursor at
        # runtime bootstrap.  A worker must never receive that reconstructed
        # history as a fresh delta.
        self._event_delta_cursor = startup_projection_cursor
        self.model_registry = model_registry
        self.work_executor = work_executor
        self.agent_naming_registry = agent_naming_registry
        self.environment_failover = environment_failover
        if work_executor is None:
            if max_parallelism is not None:
                raise InvariantViolation(
                    "max_parallelism requires an explicit WorkExecutor"
                )
            self._pool = None
        else:
            if max_parallelism is None or max_parallelism <= 0:
                raise InvariantViolation(
                    "production WorkExecutor requires positive max_parallelism"
                )
            self._pool = ThreadPoolExecutor(max_workers=max_parallelism)
        self._lock = RLock()
        self._futures: set[Future[WorkExecutionOutcome]] = set()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=False)

    def dispatch_ready(
        self,
        bindings: tuple[PilotBinding, ...],
        *,
        actor_id: str,
        idempotency_key: str,
        allowed_work_item_ids: frozenset[str] | None = None,
    ) -> DispatchBatch:
        self.kernel.activation.require_active("dispatch")
        if self.model_registry is None or self.work_executor is None:
            raise InvariantViolation(
                "dispatch requires an explicit production WorkExecutor"
            )
        binding_by_node = {binding.node_id: binding for binding in bindings}
        if len(binding_by_node) != len(bindings):
            raise InvariantViolation("PilotBinding node identities must be unique")
        published_by_route: dict[str, RouteSnapshot] = {}
        for snapshot in self.kernel.route_snapshots.values():
            if snapshot.state is not RouteSnapshotState.PUBLISHED:
                continue
            if snapshot.route_key in published_by_route:
                raise InvariantViolation(
                    f"multiple published RouteSnapshots for {snapshot.route_key}"
                )
            published_by_route[snapshot.route_key] = snapshot
        active_work_ids = {
            execution.work_item_id
            for execution in self.kernel.executions.values()
            if execution.state in (ExecutionState.REQUESTED, ExecutionState.ACTIVE)
        }
        eligible: list[WorkItem] = []
        for work in sorted(
            self.kernel.work_items.values(), key=lambda item: item.work_item_id
        ):
            if (
                allowed_work_item_ids is not None
                and work.work_item_id not in allowed_work_item_ids
            ):
                continue
            if work.state is not WorkState.READY or work.work_item_id in active_work_ids:
                continue
            dependencies = (
                edge.target_work_item_id
                for edge in self.kernel.work_edges
                if (
                    edge.source_work_item_id == work.work_item_id
                    and edge.kind is WorkEdgeKind.DEPENDS_ON
                )
            )
            if any(
                self.kernel.work_items[item_id].state is not WorkState.COMPLETED
                for item_id in dependencies
            ):
                continue
            eligible.append(work)
        planned: list[tuple[WorkItem, PilotBinding, ModelCandidate]] = []
        for work in eligible:
            binding = binding_by_node.get(work.delegated_to_node_id)
            if binding is None:
                raise InvariantViolation(
                    f"no PilotBinding for delegated Node {work.delegated_to_node_id}"
                )
            snapshot = published_by_route.get(work.route_key)
            if snapshot is None:
                raise InvariantViolation(
                    f"no published RouteSnapshot for {work.route_key}"
                )
            if snapshot.evidence_cursor != self.evidence_cursor():
                raise InvariantViolation(
                    f"published RouteSnapshot is stale for {work.route_key}"
                )
            selected = self.router.select_production(snapshot)
            candidate = self.model_registry.get(selected.candidate_key)
            if candidate is None:
                raise InvariantViolation(
                    "RouteSnapshot selected an unregistered ModelCandidate"
                )
            self.work_executor.validate_work_candidate(candidate)
            planned.append((work, binding, candidate))

        executions: list[tuple[Execution, WorkItem, ModelCandidate]] = []
        assignments: list[DispatchAssignment] = []
        for work, binding, candidate in planned:
            execution_id = new_id("execution")
            assignment = None
            if self.agent_naming_registry is not None:
                assignment = self.agent_naming_registry.allocate(
                    assignment_id=new_id("assignment"),
                    data_space_id=work.data_space_id,
                    work_item_id=work.work_item_id,
                    execution_id=execution_id,
                    node_id=work.delegated_to_node_id,
                    pilot_id=binding.pilot_id,
                    candidate=candidate,
                )
            execution = Execution(
                execution_id=execution_id,
                data_space_id=work.data_space_id,
                node_id=work.delegated_to_node_id,
                work_item_id=work.work_item_id,
                pilot_id=binding.pilot_id,
                model_candidate_key=candidate.key,
                state=ExecutionState.REQUESTED,
                provider_session_id=None,
                pilot_host_id=binding.pilot_host_id,
                pause_reason=None,
            )
            self.kernel.create_execution(
                execution,
                actor_id=actor_id,
                idempotency_key=f"{idempotency_key}:{work.work_item_id}",
            )
            if assignment is not None:
                self.kernel.record_agent_name_assignment(
                    assignment,
                    naming_registry=self.agent_naming_registry,
                    actor_id="system:dispatcher",
                    idempotency_key=(
                        f"{idempotency_key}:{execution.execution_id}:agent-name"
                    ),
                )
                execution = execution.model_copy(
                    update={"agent_name": assignment.agent_name}
                )
            executions.append((execution, work, candidate))
            assignments.append(
                DispatchAssignment(
                    work_item_id=work.work_item_id,
                    execution_id=execution.execution_id,
                    pilot_id=execution.pilot_id,
                    pilot_host_id=execution.pilot_host_id,
                    model_candidate_key=execution.model_candidate_key,
                    agent_name=(
                        assignment.agent_name if assignment is not None else None
                    ),
                )
            )

        delta = self._event_delta()
        if executions:
            if self._pool is None:
                raise InvariantViolation("production dispatch pool is unavailable")
            for execution, work, candidate in executions:
                future = self._pool.submit(
                    self.work_executor.execute_work,
                    execution_id=execution.execution_id,
                    work_item=work,
                    candidate=candidate,
                    event_delta=delta,
                    artifact_refs=(),
                    idempotency_key=(
                        f"{idempotency_key}:{execution.execution_id}:pilot"
                    ),
                    agent_name=execution.agent_name,
                )
                with self._lock:
                    self._futures.add(future)
                future.add_done_callback(
                    lambda completed, current=execution: self._finish_execution(
                        current, completed, idempotency_key
                    )
                )
        return DispatchBatch(
            assignments=tuple(assignments),
            parallelism=len(assignments),
            model_calls=len(assignments),
        )

    def preflight_ready(
        self,
        bindings: tuple[PilotBinding, ...],
        *,
        allowed_work_item_ids: frozenset[str],
        allow_owner_confirmed_prepare: bool = False,
    ) -> None:
        if (
            not allowed_work_item_ids
            or self.model_registry is None
            or self.work_executor is None
        ):
            raise InvariantViolation(
                "resume dispatch requires work IDs and production WorkExecutor"
            )
        binding_by_node = {binding.node_id: binding for binding in bindings}
        if len(binding_by_node) != len(bindings):
            raise InvariantViolation("PilotBinding node identities must be unique")
        published = {
            snapshot.route_key: snapshot
            for snapshot in self.kernel.route_snapshots.values()
            if snapshot.state is RouteSnapshotState.PUBLISHED
        }
        if len(published) != sum(
            snapshot.state is RouteSnapshotState.PUBLISHED
            for snapshot in self.kernel.route_snapshots.values()
        ):
            raise InvariantViolation("multiple published RouteSnapshots for a route")
        startable = 0
        for work_item_id in sorted(allowed_work_item_ids):
            work = self.kernel.work_items.get(work_item_id)
            allowed_states = (
                (WorkState.PROPOSED, WorkState.READY, WorkState.PAUSED)
                if allow_owner_confirmed_prepare
                else (WorkState.READY,)
            )
            if work is None or work.state not in allowed_states:
                raise InvariantViolation(
                    f"resume WorkItem is not ready: {work_item_id}"
                )
            if work.delegated_to_node_id not in binding_by_node:
                raise InvariantViolation(
                    f"resume WorkItem has no PilotBinding: {work_item_id}"
                )
            dependencies = [
                edge.target_work_item_id
                for edge in self.kernel.work_edges
                if (
                    edge.source_work_item_id == work_item_id
                    and edge.kind is WorkEdgeKind.DEPENDS_ON
                )
            ]
            if any(
                dependency not in allowed_work_item_ids
                and self.kernel.work_items[dependency].state
                is not WorkState.COMPLETED
                for dependency in dependencies
            ):
                raise InvariantViolation(
                    f"resume WorkItem has an unavailable dependency: {work_item_id}"
                )
            if all(
                self.kernel.work_items[dependency].state is WorkState.COMPLETED
                for dependency in dependencies
            ):
                startable += 1
            snapshot = published.get(work.route_key)
            if (
                snapshot is None
                or snapshot.evidence_cursor != self.evidence_cursor()
            ):
                raise InvariantViolation(
                    f"resume WorkItem route is unavailable or stale: {work_item_id}"
                )
            selected = self.router.select_production(snapshot)
            candidate = self.model_registry.get(selected.candidate_key)
            if candidate is None:
                raise InvariantViolation(
                    "resume route selected an unregistered candidate"
                )
            self.work_executor.validate_work_candidate(candidate)
        if startable == 0:
            raise InvariantViolation(
                "resume selection has no dependency-ready WorkItem"
            )

    def _finish_execution(
        self,
        execution: Execution,
        future: Future[WorkExecutionOutcome],
        idempotency_key: str,
    ) -> None:
        try:
            outcome = future.result()
        except (
            PilotHostTransportUnknown,
            PilotHostModelMismatch,
            PilotHostReceiptError,
        ) as exc:
            with self._lock:
                self._record_receipt(execution, exc.receipt, idempotency_key)
        except PilotHostUnreachable as exc:
            candidate = self.model_registry[execution.model_candidate_key]
            with self._lock:
                self.kernel.record_pilot_execution_receipt(
                    execution.execution_id,
                    receipt_id=exc.receipt_id,
                    receipt_status="transport_unknown",
                    requested_model=candidate.model_snapshot,
                    actual_model=None,
                    provider_session_id=None,
                    usage=None,
                    result=None,
                    error={
                        "code": "ReceiptUnreachable",
                        "message": str(exc),
                    },
                    actor_id=execution.pilot_id,
                    idempotency_key=(
                        f"{idempotency_key}:{execution.execution_id}:"
                        "receipt-unreachable"
                    ),
                )
                if self.environment_failover is not None:
                    self.failover_environment(
                        failed_instance_id=execution.pilot_host_id,
                        idempotency_key=(
                            f"{idempotency_key}:{execution.execution_id}:environment-failover"
                        ),
                    )
        except Exception as exc:
            # Any exception reaching here is, by definition, one the transport
            # and receipt boundaries above did not anticipate. It must never be
            # dropped silently: the traceback is the only diagnostic trail once
            # the provider process and its stdout/stderr are gone. Record it as
            # a receipt (same shape as the PilotHostUnreachable path) so it also
            # lands in the Ledger; fall back to a state-only failure transition
            # if the receipt itself cannot be recorded so the Execution never
            # hangs REQUESTED forever.
            logger.exception(
                "unexpected failure finishing dispatch for execution %s",
                execution.execution_id,
            )
            with self._lock:
                try:
                    candidate = self.model_registry[execution.model_candidate_key]
                    self.kernel.record_pilot_execution_receipt(
                        execution.execution_id,
                        receipt_id=new_id("receipt"),
                        receipt_status="failed",
                        requested_model=candidate.model_snapshot,
                        actual_model=None,
                        provider_session_id=None,
                        usage=None,
                        result=None,
                        error={
                            "code": "UnexpectedDispatchFailure",
                            "message": str(exc),
                        },
                        actor_id=execution.pilot_id,
                        idempotency_key=(
                            f"{idempotency_key}:{execution.execution_id}:"
                            "unexpected-dispatch-failure"
                        ),
                    )
                except Exception:
                    logger.exception(
                        "could not record an UnexpectedDispatchFailure receipt "
                        "for execution %s; falling back to a state-only failure "
                        "transition",
                        execution.execution_id,
                    )
                    self.kernel.set_execution_state(
                        execution.execution_id,
                        ExecutionState.FAILED,
                        actor_type="system",
                        actor_id=execution.pilot_id,
                        idempotency_key=(
                            f"{idempotency_key}:{execution.execution_id}:"
                            "unexpected-dispatch-failure"
                        ),
                        pause_reason=None,
                    )
        finally:
            with self._lock:
                self._futures.discard(future)
        if "outcome" in locals():
            with self._lock:
                self._record_receipt(execution, outcome.receipt, idempotency_key)

    def failover_environment(
        self, *, failed_instance_id: str, idempotency_key: str
    ) -> EnvironmentInstance | None:
        """Invoke the injected EnvironmentInstance failover boundary.

        PilotHost IDs are used as the instance IDs at this connection point;
        an integration that keeps separate identities can inject a translating
        adapter.  No owner approval is part of this path.
        """

        if self.environment_failover is None:
            raise InvariantViolation(
                "environment failover is not configured for this dispatcher"
            )
        return self.environment_failover.failover(
            failed_instance_id,
            idempotency_key=idempotency_key,
        )

    def wait_for_idle(self) -> None:
        while True:
            with self._lock:
                pending = tuple(self._futures)
            if not pending:
                return
            wait(pending)

    def _record_receipt(
        self,
        execution: Execution,
        receipt: PilotHostReceipt,
        idempotency_key: str,
    ) -> None:
        self.kernel.record_pilot_execution_receipt(
            execution.execution_id,
            receipt_id=receipt.receipt_id,
            receipt_status=receipt.status,
            requested_model=receipt.requested_model,
            actual_model=receipt.actual_model,
            provider_session_id=receipt.provider_session_id,
            usage=receipt.usage,
            result=receipt.result,
            error=(
                None
                if receipt.error is None
                else receipt.error.model_dump(mode="json")
            ),
            actor_id=execution.pilot_id,
            idempotency_key=(
                f"{idempotency_key}:{execution.execution_id}:receipt"
            ),
        )

    def _event_delta(self) -> EventDeltaSummary:
        # A dispatch batch can be prepared concurrently with completion
        # callbacks.  Serialising cursor allocation makes each delta a
        # disjoint ledger interval; no worker can be handed an already-sent
        # historical interval.
        with self._lock:
            after_cursor = self._event_delta_cursor
            cursor = after_cursor
            counts: Counter[str] = Counter()
            streams: set[str] = set()
            while True:
                page = self.kernel.ledger.page(cursor, 500)
                if not page:
                    break
                for stored in page:
                    if stored.cursor != cursor + 1:
                        raise InvariantViolation(
                            "Event Ledger cursor is not contiguous"
                        )
                    counts[stored.event.event_type] += 1
                    streams.add(stored.event.stream_id)
                    cursor = stored.cursor
            self._event_delta_cursor = cursor
            return EventDeltaSummary(
                after_cursor=after_cursor,
                through_cursor=cursor,
                event_count=sum(counts.values()),
                event_type_counts=dict(sorted(counts.items())),
                changed_stream_ids=tuple(sorted(streams)),
            )
