from __future__ import annotations

import hashlib
import json
import math

import pytest

from conftest import INTERFACE_NODE_ID, NOW, OWNER_ID, SPACE_ID, make_node
from vsm.activation.models import (
    ActivationState,
    CurrentWorkGraphSnapshot,
    EvidenceCitation,
    HistoryImportReceipt,
    HistorySession,
    HistorySourceKind,
    HistorySourceManifest,
    ReorientationAssessment,
)
from vsm.activation.reorientation import (
    HistoryPage,
    ReorientationAssessmentContract,
    ReorientationAssessmentContractReference,
    HistoryToolService,
    ReorientationService,
)
from vsm.dispatcher import DependencyAwareDispatcher, PilotBinding
from vsm.errors import InvariantViolation
from vsm.interface.models import (
    Conversation,
    ReadHistoryAction,
    SubmitReorientationAction,
    SurfaceBinding,
)
from vsm.interface.service import InterfaceService
from vsm.kernel.models import (
    Execution,
    ExecutionState,
    NodeKind,
    RouteSnapshot,
    RouteSnapshotState,
    WorkItem,
    WorkState,
)
from vsm.pilot.claude import ClaudePilotAdapter
from vsm.pilot.models import (
    CacheOpportunity,
    InterfacePilotUsage,
    JudgeKind,
    JudgeObservation,
    ModelCandidate,
    PilotMode,
    PilotPolicy,
    ProviderSession,
    StructuredInterfaceResponse,
)
from vsm.pilot.production_host import (
    PilotHostReceipt,
    WorkExecutionOutcome,
    WorkExecutionResult,
)
from vsm.kernel.service import Kernel
from vsm.projection import OperationalProjection
from vsm.routing.bayesian import BayesianRouter, BenchmarkPrior
from vsm.token_lab.lab import TokenEfficiencyLab, TokenLabEventService


def history_contract(ledger):
    raw = b"past owner decision"
    session = HistorySession(
        session_ref="history-session:claude",
        source_session_id="claude-root",
        source_kind=HistorySourceKind.CLAUDE_CODE,
        source_id="claude_code:local",
        message_count=1,
        first_message_at=NOW,
        last_message_at=NOW,
    )
    sources = []
    for kind in HistorySourceKind:
        source_sessions = (
            (session,) if kind is HistorySourceKind.CLAUDE_CODE else ()
        )
        sources.append(
            HistorySourceManifest(
                source_id=(
                    "claude_code:local"
                    if kind is HistorySourceKind.CLAUDE_CODE
                    else f"{kind.value}:local"
                ),
                source_kind=kind,
                ownership="personal",
                owner_id=OWNER_ID,
                record_count=sum(item.message_count for item in source_sessions),
                raw_bytes=len(raw) if source_sessions else 0,
                digest_sha256=hashlib.sha256(kind.value.encode()).hexdigest(),
                cutover_cursor=f"cursor-{kind.value}",
            )
        )
    receipt = HistoryImportReceipt(
        schema="schema:history-activation-handoff",
        schema_version="1.0.0",
        inventory_id="history-import:primary",
        data_space_id=SPACE_ID,
        manifest_digest=hashlib.sha256(b"manifest").hexdigest(),
        record_count=sum(source.record_count for source in sources),
        raw_bytes=sum(source.raw_bytes for source in sources),
        cross_source_overlap_identities=0,
        sources=tuple(sources),
        session_count=1,
        sessions=(session,),
        session_index_ref=f"history-projection:sessions:sha256:{'a' * 64}",
        open_commitments_ref=(
            f"history-projection:commitments:sha256:{'b' * 64}"
        ),
        current_state_ref=f"history-projection:state:sha256:{'c' * 64}",
    )
    return receipt


def import_work_graph(kernel):
    snapshot = CurrentWorkGraphSnapshot(
        snapshot_id="work-graph:cutover",
        data_space_id=SPACE_ID,
        captured_at=NOW,
        nodes=(),
        work_items=(
            WorkItem(
                work_item_id="work:historical-resume",
                data_space_id=SPACE_ID,
                title="Historical real work",
                description="Resume the captured incomplete work.",
                owner_node_id=INTERFACE_NODE_ID,
                delegated_to_node_id=INTERFACE_NODE_ID,
                integration_owner_node_id=INTERFACE_NODE_ID,
                parent_work_item_id=None,
                acceptance_criteria=("captured acceptance",),
                route_key="coding_s1",
                state=WorkState.PAUSED,
                blocking_s3_star_finding_ids=(),
                completion_evidence=None,
            ),
        ),
        edges=(),
        snapshot_sha256="0" * 64,
    )
    snapshot = snapshot.model_copy(
        update={"snapshot_sha256": snapshot.calculated_sha256()}
    )
    kernel.import_current_work_graph(
        snapshot,
        actor_id=OWNER_ID,
        idempotency_key="work-graph:import",
    )
    return snapshot


def test_work_graph_digest_canonicalizes_resident_function_set():
    snapshot = CurrentWorkGraphSnapshot(
        snapshot_id="work-graph:digest-test",
        data_space_id=SPACE_ID,
        captured_at=NOW,
        nodes=(
            make_node(
                INTERFACE_NODE_ID,
                name="Owner Interface",
                kind=NodeKind.INTERFACE,
            ),
        ),
        work_items=(
            WorkItem(
                work_item_id="work:digest-test",
                data_space_id=SPACE_ID,
                title="Digest test",
                description="Verify deterministic Work Graph hashing.",
                owner_node_id=INTERFACE_NODE_ID,
                delegated_to_node_id=INTERFACE_NODE_ID,
                integration_owner_node_id=INTERFACE_NODE_ID,
                parent_work_item_id=None,
                acceptance_criteria=("digest is stable",),
                route_key="coding_s1",
                state=WorkState.PAUSED,
                blocking_s3_star_finding_ids=(),
                completion_evidence=None,
            ),
        ),
        edges=(),
        snapshot_sha256="0" * 64,
    )
    canonical_payload = snapshot.model_dump(
        mode="json", exclude={"snapshot_sha256"}
    )
    canonical_payload["nodes"][0]["resident_functions"] = sorted(
        canonical_payload["nodes"][0]["resident_functions"]
    )
    expected = hashlib.sha256(
        json.dumps(
            canonical_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    assert snapshot.calculated_sha256() == expected


class FakeHistoryReader:
    def _page(self, value):
        return HistoryPage(result_json=value, next_cursor=None, source_cursor="lethe-7")

    def list_sessions(self, *, page_cursor):
        return self._page([{"session_ref": "history-session:claude"}])

    def read_timeline(self, session_id, *, page_cursor):
        return self._page([{"message_id": "history-message:one"}])

    def read_raw(self, message_id, *, page_cursor):
        return self._page({"message_id": message_id, "text": "past owner decision"})

    def search(self, query, *, page_cursor):
        return self._page([{"message_id": "history-message:one"}])

    def resolve_reference(self, reference_id, *, page_cursor):
        return self._page({"reference_id": reference_id})

    def list_open_commitments(self, *, page_cursor):
        return self._page([])

    def get_current_state(self, *, state_key, page_cursor):
        return self._page({"work_items": [], "executions": []})


def usage():
    return InterfacePilotUsage(
        candidate_key="candidate:fake",
        actual_provider="fake",
        actual_model_snapshot="fake",
        input_tokens=10,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        output_tokens=5,
        cost_usd=0,
        duration_ms=1,
        classifier_triggered=False,
        model_substitution=False,
        full_history_resent=False,
        polling_call=False,
        false_complete=False,
        reedited_tokens=0,
    )


class DrillDownPilot:
    def __init__(self, kernel):
        self.kernel = kernel
        self.calls = 0
        self.sessions = []
        self.contexts = []

    def respond_reorientation(self, context):
        self.calls += 1
        self.sessions.append(context.provider_session_id)
        self.contexts.append(context)
        if self.calls == 1:
            actions = (
                ReadHistoryAction(
                    action_id="history-action:raw",
                    kind="history.read",
                    operation="read_raw",
                    argument="history-message:one",
                    page_cursor=None,
                ),
            )
        elif self.calls == 2:
            actions = (
                ReadHistoryAction(
                    action_id="history-action:state",
                    kind="history.read",
                    operation="get_current_state",
                    argument=None,
                    page_cursor=None,
                ),
            )
        else:
            assessment = ReorientationAssessment(
                assessment_id="assessment:primary",
                import_id="history-import:primary",
                conversation_id="conversation:reorientation",
                generated_at=NOW,
                understanding="The owner has an unresolved mission.",
                active_missions=(),
                decisions_and_constraints=(),
                open_commitment_ids=(),
                unknowns=(),
                resume_work_item_ids=("work:historical-resume",),
                covered_session_index_ref="history-projection:sessions:sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                covered_session_count=1,
                history_cursor=self.kernel.activation.import_event_cursor,
                current_state_cursor=context.history_result.event_cursor,
                citations=(
                    EvidenceCitation(
                        claim_ref="understanding",
                        evidence_ref=context.history_result.result_event_id,
                    ),
                ),
            )
            actions = (
                SubmitReorientationAction(
                    action_id="reorientation-action:submit",
                    kind="reorientation.submit",
                    assessment=assessment,
                ),
            )
        return StructuredInterfaceResponse(
            display_text="reorienting",
            actions=actions,
            provider_session_id=f"provider-leaf-{self.calls}",
            pilot_usage=usage(),
        )


class CheckpointGatePilot:
    def __init__(self, kernel, *, provider_session_id, valid):
        self.kernel = kernel
        self.provider_session_id = provider_session_id
        self.valid = valid
        self.contexts = []

    def respond_reorientation(self, context):
        self.contexts.append(context)
        assessment = ReorientationAssessment(
            assessment_id=(
                "assessment:checkpoint-valid"
                if self.valid
                else "assessment:checkpoint-invalid"
            ),
            import_id="history-import:primary",
            conversation_id="conversation:reorientation",
            generated_at=NOW,
            understanding="Resume the verified reorientation session.",
            active_missions=(),
            decisions_and_constraints=(),
            open_commitment_ids=(),
            unknowns=(),
            resume_work_item_ids=(
                ("work:historical-resume",) if self.valid else ()
            ),
            covered_session_index_ref="history-projection:sessions:sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            covered_session_count=1 if self.valid else 0,
            history_cursor=self.kernel.activation.import_event_cursor,
            current_state_cursor=context.history_result.event_cursor,
            citations=(
                EvidenceCitation(
                    claim_ref="understanding",
                    evidence_ref=context.history_result.result_event_id,
                ),
            ),
        )
        return StructuredInterfaceResponse(
            display_text="checkpointed",
            actions=(
                SubmitReorientationAction(
                    action_id=(
                        "reorientation-action:checkpoint-valid"
                        if self.valid
                        else "reorientation-action:checkpoint-invalid"
                    ),
                    kind="reorientation.submit",
                    assessment=assessment,
                ),
            ),
            provider_session_id=self.provider_session_id,
            pilot_usage=usage(),
        )


def commission_history(kernel, ledger):
    kernel.activation.state = ActivationState.UNCOMMISSIONED
    import_work_graph(kernel)
    receipt = history_contract(ledger)
    kernel.activation.register_history_import(
        receipt,
        reorientation_conversation_id="conversation:reorientation",
        actor_id=OWNER_ID,
        idempotency_key="history:import",
    )
    return receipt


def test_receipt_mismatch_and_owner_confirmation_gate(system):
    kernel, ledger, _, _ = system
    kernel.activation.state = ActivationState.UNCOMMISSIONED
    import_work_graph(kernel)
    receipt = history_contract(ledger)
    bad_source = receipt.sources[0].model_copy(
        update={"record_count": receipt.sources[0].record_count + 1}
    )
    bad = receipt.model_copy(update={"sources": (bad_source, *receipt.sources[1:])})
    with pytest.raises(InvariantViolation, match="Receipt"):
        kernel.activation.register_history_import(
            bad,
            reorientation_conversation_id="conversation:reorientation",
            actor_id=OWNER_ID,
            idempotency_key="history:bad",
        )
    kernel.activation.register_history_import(
        receipt,
        reorientation_conversation_id="conversation:reorientation",
        actor_id=OWNER_ID,
        idempotency_key="history:good",
    )
    assert kernel.activation.state is ActivationState.HISTORY_IMPORTED
    assert (
        kernel.activation.status().reorientation_conversation_id
        == "conversation:reorientation"
    )
    execution = Execution(
        execution_id="execution:forbidden",
        data_space_id=SPACE_ID,
        node_id=INTERFACE_NODE_ID,
        work_item_id="work:not-created",
        pilot_id="pilot:test",
        model_candidate_key="candidate:test",
        state=ExecutionState.REQUESTED,
        provider_session_id=None,
        pilot_host_id="pilot-host:test",
        pause_reason=None,
    )
    with pytest.raises(InvariantViolation, match="before owner-confirmed activation"):
        kernel.create_execution(
            execution, actor_id=OWNER_ID, idempotency_key="execution:forbidden"
        )


def test_interface_pilot_drills_down_with_bounded_results_then_waits_for_owner(system):
    kernel, ledger, interface, interface_pilot = system
    interface.create_conversation(
        Conversation(
            conversation_id="conversation:reorientation",
            data_space_id=SPACE_ID,
            interface_node_id=INTERFACE_NODE_ID,
            owner_id=OWNER_ID,
            title="Reorientation",
        ),
        SurfaceBinding(
            binding_id="binding:reorientation",
            conversation_id="conversation:reorientation",
            surface="web",
            source_session_id="browser-session",
            channel_id="owner",
            device_id="device:owner",
        ),
        idempotency_key="conversation:reorientation",
    )
    commission_history(kernel, ledger)
    kernel.activation.start_reorientation(
        actor_id="pilot:interface", idempotency_key="reorientation:start"
    )
    kernel.activation.record_reorientation_failure(
        error_code="ProviderTimeout",
        actor_id="pilot:interface",
        idempotency_key="reorientation:failure",
    )
    assert kernel.activation.status().reorientation_error == "ProviderTimeout"
    assert not kernel.executions
    assert not kernel.effect_leases
    kernel.activation.start_reorientation(
        actor_id="pilot:interface", idempotency_key="reorientation:retry"
    )
    assert kernel.activation.status().reorientation_error is None
    pilot = DrillDownPilot(kernel)
    service = ReorientationService(
        kernel=kernel,
        interface=interface,
        pilot=pilot,
        history_reader=FakeHistoryReader(),
        max_result_bytes=4096,
    )
    service.execute(
        initial_action=ReadHistoryAction(
            action_id="history-action:list",
            kind="history.read",
            operation="get_current_state",
            argument=None,
            page_cursor=None,
        ),
        actor_id="pilot:interface",
        idempotency_key="reorientation:loop",
        max_tool_rounds=3,
        objective="Reorient",
        session_index_ref="history-index:all-sessions",
        open_commitment_refs=(
            f"history-projection:commitments:sha256:{'b' * 64}",
        ),
        current_state_ref="history-index:current-state",
    )
    assert pilot.calls == 3
    assert pilot.sessions == [None, "provider-leaf-1", "provider-leaf-2"]
    assert pilot.contexts[0].assessment_contract_included is True
    assert isinstance(pilot.contexts[0].assessment_contract, ReorientationAssessmentContract)
    assert [
        item.model_dump(mode="json")
        for item in pilot.contexts[0].assessment_contract.resume_work_items
    ] == [
        {
            "work_item_id": "work:historical-resume",
            "title": "Historical real work",
            "description": "Resume the captured incomplete work.",
            "acceptance_criteria": ["captured acceptance"],
            "state": "paused",
        }
    ]
    assert pilot.contexts[1].assessment_contract_included is False
    assert isinstance(
        pilot.contexts[1].assessment_contract,
        ReorientationAssessmentContractReference,
    )
    assert (
        pilot.contexts[1].assessment_contract.resume_work_items
        == pilot.contexts[0].assessment_contract.resume_work_items
    )
    assert "history-session:claude" not in json.dumps(
        pilot.contexts[1].assessment_contract.model_dump(mode="json"),
        ensure_ascii=False,
    )
    assert kernel.activation.state is ActivationState.AWAITING_OWNER_CONFIRMATION
    assert len(interface.node_memories) == 1
    status = kernel.activation.status()
    assert status.status_model_calls == 0
    assert status.reorientation_pilot_calls == 3
    kernel.activation.approve(
        "assessment:primary",
        actor_id=OWNER_ID,
        idempotency_key="reorientation:approve",
    )
    assert kernel.activation.state is ActivationState.ACTIVE

    rebuilt_kernel = Kernel(
        data_space=kernel.data_space,
        ledger=ledger,
        audit_policy=kernel.audit_policy,
        control_policy=kernel.control_policy,
        clock=kernel.clock,
    )
    rebuilt_token_events = TokenLabEventService(
        lab=TokenEfficiencyLab(),
        ledger=ledger,
        data_space_id=SPACE_ID,
        clock=kernel.clock,
    )
    rebuilt_interface = InterfaceService(
        kernel=rebuilt_kernel,
        ledger=ledger,
        pilot=interface_pilot,
        token_lab_events=rebuilt_token_events,
        clock=kernel.clock,
    )
    OperationalProjection(
        kernel=rebuilt_kernel,
        interface=rebuilt_interface,
        token_lab_events=rebuilt_token_events,
    ).rebuild()
    rebuilt_status = rebuilt_kernel.activation.status()
    assert rebuilt_status.state is ActivationState.ACTIVE
    assert rebuilt_status.reorientation_pilot_calls == 3
    assert set(rebuilt_kernel.activation.sessions) == {"history-session:claude"}
    assert len(rebuilt_interface.node_memories) == 1
    assert (
        rebuilt_status.reorientation_conversation_id
        == "conversation:reorientation"
    )
    rebuilt_session = next(iter(rebuilt_interface.pilot_sessions.values()))
    assert rebuilt_session.root_provider_session_id == "provider-leaf-3"
    assert rebuilt_session.provider_session_id == "provider-leaf-3"


def test_gate_failure_retry_resumes_durable_provider_session_without_full_contract(
    system,
):
    kernel, ledger, interface, interface_pilot = system
    interface.create_conversation(
        Conversation(
            conversation_id="conversation:reorientation",
            data_space_id=SPACE_ID,
            interface_node_id=INTERFACE_NODE_ID,
            owner_id=OWNER_ID,
            title="Reorientation",
        ),
        SurfaceBinding(
            binding_id="binding:reorientation",
            conversation_id="conversation:reorientation",
            surface="web",
            source_session_id="browser-session",
            channel_id="owner",
            device_id="device:owner",
        ),
        idempotency_key="conversation:reorientation",
    )
    commission_history(kernel, ledger)
    kernel.activation.start_reorientation(
        actor_id="pilot:interface",
        idempotency_key="reorientation:checkpoint:start",
    )
    failing_pilot = CheckpointGatePilot(
        kernel,
        provider_session_id="provider-root",
        valid=False,
    )
    failing_service = ReorientationService(
        kernel=kernel,
        interface=interface,
        pilot=failing_pilot,
        history_reader=FakeHistoryReader(),
        max_result_bytes=4096,
    )

    with pytest.raises(InvariantViolation, match="session count differs"):
        failing_service.execute(
            initial_action=ReadHistoryAction(
                action_id="history-action:checkpoint-initial",
                kind="history.read",
                operation="get_current_state",
                argument=None,
                page_cursor=None,
            ),
            actor_id="pilot:interface",
            idempotency_key="reorientation:checkpoint:first-attempt",
            max_tool_rounds=1,
            objective="Reorient",
            session_index_ref="history-index:all-sessions",
            open_commitment_refs=(),
            current_state_ref="history-index:current-state",
        )

    assert failing_pilot.contexts[0].provider_session_id is None
    assert isinstance(
        failing_pilot.contexts[0].assessment_contract,
        ReorientationAssessmentContract,
    )
    assert kernel.activation.reorientation_provider_session_id == "provider-root"
    event_types = [
        stored.event.event_type for stored in ledger.page(0, 10_000)
    ]
    checkpoint_index = event_types.index(
        "reorientation_session_checkpointed"
    )
    usage_index = event_types.index("reorientation_pilot_usage_recorded")
    assert checkpoint_index < usage_index

    kernel.activation.record_reorientation_failure(
        error_code="AssessmentGateRejected",
        actor_id="system:reorientation",
        idempotency_key="reorientation:checkpoint:failure",
    )
    rebuilt_kernel = Kernel(
        data_space=kernel.data_space,
        ledger=ledger,
        audit_policy=kernel.audit_policy,
        control_policy=kernel.control_policy,
        clock=kernel.clock,
    )
    rebuilt_token_events = TokenLabEventService(
        lab=TokenEfficiencyLab(),
        ledger=ledger,
        data_space_id=SPACE_ID,
        clock=kernel.clock,
    )
    rebuilt_interface = InterfaceService(
        kernel=rebuilt_kernel,
        ledger=ledger,
        pilot=interface_pilot,
        token_lab_events=rebuilt_token_events,
        clock=kernel.clock,
    )
    OperationalProjection(
        kernel=rebuilt_kernel,
        interface=rebuilt_interface,
        token_lab_events=rebuilt_token_events,
    ).rebuild()

    assert (
        rebuilt_kernel.activation.reorientation_provider_session_id
        == "provider-root"
    )
    rebuilt_kernel.activation.start_reorientation(
        actor_id="pilot:interface",
        idempotency_key="reorientation:checkpoint:retry",
    )
    resumed_pilot = CheckpointGatePilot(
        rebuilt_kernel,
        provider_session_id="provider-fork",
        valid=True,
    )
    resumed_service = ReorientationService(
        kernel=rebuilt_kernel,
        interface=rebuilt_interface,
        pilot=resumed_pilot,
        history_reader=FakeHistoryReader(),
        max_result_bytes=4096,
    )
    resumed_service.execute(
        initial_action=ReadHistoryAction(
            action_id="history-action:checkpoint-retry",
            kind="history.read",
            operation="get_current_state",
            argument=None,
            page_cursor=None,
        ),
        actor_id="pilot:interface",
        idempotency_key="reorientation:checkpoint:second-attempt",
        max_tool_rounds=1,
        objective="Reorient",
        session_index_ref="history-index:all-sessions",
        open_commitment_refs=(),
        current_state_ref="history-index:current-state",
    )

    resumed_context = resumed_pilot.contexts[0]
    assert resumed_context.provider_session_id == "provider-root"
    assert resumed_context.assessment_contract_included is False
    assert isinstance(
        resumed_context.assessment_contract,
        ReorientationAssessmentContractReference,
    )
    assert (
        resumed_context.assessment_contract.covered_session_index_ref
        == "history-projection:sessions:sha256:"
        + "a" * 64
    )
    assert resumed_context.assessment_contract.covered_session_count == 1
    assert resumed_context.assessment_contract.open_commitment_ids == ()
    assert [
        item.work_item_id
        for item in resumed_context.assessment_contract.resume_work_items
    ] == ["work:historical-resume"]
    assert resumed_context.assessment_contract.minimum_history_cursor > 0
    assert "history-session:claude" not in json.dumps(
        resumed_context.assessment_contract.model_dump(mode="json"),
        ensure_ascii=False,
    )
    assert (
        rebuilt_kernel.activation.reorientation_provider_session_id
        == "provider-fork"
    )
    canonical_session = next(iter(rebuilt_interface.pilot_sessions.values()))
    assert canonical_session.provider_session_id == "provider-fork"


def test_assessment_cannot_switch_the_imported_canonical_conversation(system):
    kernel, ledger, _, _ = system
    commission_history(kernel, ledger)
    kernel.activation.start_reorientation(
        actor_id="pilot:interface", idempotency_key="reorientation:start"
    )

    assessment = ReorientationAssessment(
        assessment_id="assessment:wrong-conversation",
        import_id="history-import:primary",
        conversation_id="conversation:other",
        generated_at=NOW,
        understanding="This must not be accepted.",
        active_missions=(),
        decisions_and_constraints=(),
        open_commitment_ids=(),
        unknowns=(),
        resume_work_item_ids=(),
        covered_session_index_ref="history-projection:sessions:sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        covered_session_count=0,
        history_cursor=0,
        current_state_cursor=0,
        citations=(),
    )

    with pytest.raises(InvariantViolation, match="imported canonical Conversation"):
        kernel.activation.submit_assessment(
            assessment,
            open_commitment_ids=(),
            existing_work_item_ids=(),
            session_index_listed_to_end=False,
            actor_id="pilot:interface",
            idempotency_key="assessment:wrong-conversation",
        )


def test_history_reader_must_paginate_instead_of_oversize_truncation(system):
    kernel, ledger, _, _ = system
    commission_history(kernel, ledger)
    kernel.activation.start_reorientation(
        actor_id="pilot:interface", idempotency_key="reorientation:start"
    )

    class Oversize(FakeHistoryReader):
        def list_sessions(self, *, page_cursor):
            return self._page([{"session_ref": "history-session:" + "x" * 100}])

    service = HistoryToolService(
        kernel=kernel, reader=Oversize(), max_result_bytes=16
    )
    with pytest.raises(InvariantViolation, match="session index page exceeded"):
        service.scan_session_index(
            receipt=kernel.activation.import_receipt,
            actor_id="system:reorientation",
            idempotency_key="history:oversize",
        )


def test_session_index_audit_does_not_invalidate_shared_ledger_cursor(system):
    kernel, ledger, _, _ = system
    commission_history(kernel, ledger)
    kernel.activation.start_reorientation(
        actor_id="pilot:interface", idempotency_key="reorientation:start"
    )

    class SnapshotCursorReader(FakeHistoryReader):
        def __init__(self):
            self.event_count_at_first_page = None

        def list_sessions(self, *, page_cursor):
            event_count = len(ledger.page(0, 10_000))
            if page_cursor is None:
                self.event_count_at_first_page = event_count
                return HistoryPage(
                    result_json=[],
                    next_cursor="cursor:second-page",
                    source_cursor=f"lethe-{event_count}",
                )
            assert page_cursor == "cursor:second-page"
            if event_count != self.event_count_at_first_page:
                raise InvariantViolation(
                    "audit write invalidated the shared-ledger continuation cursor"
                )
            return HistoryPage(
                result_json=[{"session_ref": "history-session:claude"}],
                next_cursor=None,
                source_cursor=f"lethe-{event_count}",
            )

    service = HistoryToolService(
        kernel=kernel,
        reader=SnapshotCursorReader(),
        max_result_bytes=4096,
    )
    results, summary = service.scan_session_index(
        receipt=kernel.activation.import_receipt,
        actor_id="system:reorientation",
        idempotency_key="history:stable-pagination",
    )

    assert len(results) == 2
    assert summary.session_count == 1
    audit_events = [
        stored.event
        for stored in ledger.page(0, 10_000)
        if stored.event.event_type == "history_session_index_page_verified"
    ]
    assert len(audit_events) == 2


def test_history_open_commitments_are_materialized_before_assessment(system):
    kernel, ledger, interface, interface_pilot = system
    interface.create_conversation(
        Conversation(
            conversation_id="conversation:reorientation",
            data_space_id=SPACE_ID,
            interface_node_id=INTERFACE_NODE_ID,
            owner_id=OWNER_ID,
            title="Reorientation",
        ),
        SurfaceBinding(
            binding_id="binding:reorientation",
            conversation_id="conversation:reorientation",
            surface="web",
            source_session_id="browser-session",
            channel_id="owner",
            device_id="device:owner",
        ),
        idempotency_key="conversation:reorientation",
    )
    commission_history(kernel, ledger)
    kernel.activation.start_reorientation(
        actor_id="pilot:interface", idempotency_key="reorientation:start"
    )

    class CommitmentReader(FakeHistoryReader):
        def list_open_commitments(self, *, page_cursor):
            return self._page(
                [
                    {
                        "commitment_id": "commitment:historical",
                        "text": "Finish the historical task.",
                        "event_id": "event:history:commitment",
                    }
                ]
            )

    service = ReorientationService(
        kernel=kernel,
        interface=interface,
        pilot=interface_pilot,
        history_reader=CommitmentReader(),
        max_result_bytes=4096,
    )
    service._materialize_open_commitments(
        actor_id="pilot:interface", idempotency_key="history:commitments"
    )

    assert set(interface.commitments) == {"commitment:historical"}
    assert interface.commitments["commitment:historical"].state == "open"


def candidate():
    return ModelCandidate(
        adapter="codex",
        adapter_version="1",
        provider="openai",
        selection="exact",
        model_snapshot="gpt-5.6-luna",
        effort="xhigh",
        toolset=("filesystem",),
        sandbox_fingerprint="sandbox:test",
        environment_fingerprint="environment:test",
    )


def router_with_verified_candidate():
    model = candidate()
    router = BayesianRouter(
        expected_utility_quality_weight=1,
        expected_utility_cost_weight=0,
        expected_utility_latency_weight=0,
    )
    router.register(
        model,
        (
            BenchmarkPrior(
                source="swe-bench",
                benchmark_family="coding",
                version="fixed",
                sample_count=2,
                harness="fixed",
                successes=1,
                failures=1,
                log_token_samples=(math.log(10),),
                log_cost_samples=(math.log(0.1),),
                log_latency_samples=(math.log(10),),
            ),
        ),
    )
    router.update_verified(
        candidate_key=model.key,
        success=True,
        tokens=10,
        cost=0.1,
        latency_ms=10,
        judge=JudgeObservation(
            candidate_key=model.key,
            kind=JudgeKind.DETERMINISTIC,
            predicted_success=True,
            verified_success=True,
            judge_model=None,
            judge_effort=None,
        ),
    )
    return router, model


def test_dispatcher_selects_published_route_and_parallelizes_independent_work(system):
    kernel, _, _, _ = system
    worker = make_node("node:worker", name="Worker", kind=NodeKind.UNIT)
    kernel.register_node(worker, actor_id=OWNER_ID, idempotency_key="node:worker")
    for suffix in ("one", "two"):
        kernel.create_work_item(
            WorkItem(
                work_item_id=f"work:{suffix}",
                data_space_id=SPACE_ID,
                title=suffix,
                description=f"Do {suffix}",
                owner_node_id=INTERFACE_NODE_ID,
                delegated_to_node_id=worker.node_id,
                integration_owner_node_id=INTERFACE_NODE_ID,
                parent_work_item_id=None,
                acceptance_criteria=("verified",),
                route_key="coding_s1",
                state=WorkState.READY,
                blocking_s3_star_finding_ids=(),
                completion_evidence=None,
            ),
            actor_id=OWNER_ID,
            idempotency_key=f"work:{suffix}",
        )
    router, model = router_with_verified_candidate()
    snapshot = RouteSnapshot(
        snapshot_id="route:dispatcher",
        data_space_id=SPACE_ID,
        route_key="coding_s1",
        evidence_cursor=0,
        candidate_keys=(model.key,),
        production_objective="quality_max",
        state=RouteSnapshotState.PUBLISHED,
        s3_star_approval_event_id="event:s3",
        owner_approval_event_id="event:owner",
    )
    kernel.route_snapshots[snapshot.snapshot_id] = snapshot
    class Executor:
        def __init__(self):
            self.calls = []

        def validate_work_candidate(self, selected):
            assert selected == model

        def execute_work(self, **kwargs):
            self.calls.append(kwargs["execution_id"])
            receipt = PilotHostReceipt(
                receipt_id=f"receipt:{len(self.calls)}",
                endpoint="/v1/work-executions",
                idempotency_key=kwargs["idempotency_key"],
                request_sha256="a" * 64,
                status="succeeded",
                candidate_key=model.key,
                requested_model=model.model_snapshot,
                actual_model=model.model_snapshot,
                provider_session_id=f"codex-session-{len(self.calls)}",
                usage={"input_tokens": 10},
                result={
                    "summary": "done",
                    "acceptance_results": [
                        {
                            "criterion": "verified",
                            "satisfied": True,
                            "evidence_refs": [],
                        }
                    ],
                    "artifact_refs": [],
                    "event_notes": [],
                    "completed": True,
                },
                error=None,
                created_at=NOW.isoformat(),
                updated_at=NOW.isoformat(),
            )
            return WorkExecutionOutcome(
                receipt=receipt,
                result=WorkExecutionResult.model_validate(receipt.result),
            )

    executor = Executor()
    dispatcher = DependencyAwareDispatcher(
        kernel=kernel,
        router=router,
        evidence_cursor=lambda: 0,
        model_registry={model.key: model},
        work_executor=executor,
        max_parallelism=2,
    )
    batch = dispatcher.dispatch_ready(
        (
            PilotBinding(
                node_id=worker.node_id,
                pilot_id="pilot:worker",
                pilot_host_id="pilot-host:worker",
            ),
        ),
        actor_id=OWNER_ID,
        idempotency_key="dispatch:parallel",
    )
    dispatcher.wait_for_idle()
    dispatcher.close()
    assert batch.parallelism == 2
    assert batch.model_calls == 2
    assert {item.model_candidate_key for item in batch.assignments} == {model.key}
    assert len(executor.calls) == 2
    assert all(
        execution.state is ExecutionState.SUCCEEDED
        for execution in kernel.executions.values()
    )


def test_history_manifest_is_bounded_for_large_record_totals(system):
    kernel, ledger, _, _ = system
    receipt = history_contract(ledger)
    first = receipt.sources[0].model_copy(
        update={"record_count": 76_725, "raw_bytes": 512_000_000}
    )
    sources = (first, *receipt.sources[1:])
    receipt = receipt.model_copy(
        update={
            "record_count": sum(source.record_count for source in sources),
            "raw_bytes": sum(source.raw_bytes for source in sources),
            "sources": sources,
        }
    )
    kernel.activation.state = ActivationState.UNCOMMISSIONED
    import_work_graph(kernel)
    kernel.activation.register_history_import(
        receipt,
        reorientation_conversation_id="conversation:reorientation",
        actor_id=OWNER_ID,
        idempotency_key="history:large-bounded",
    )
    stored = ledger.page(0, 100)[-1]
    assert len(stored.event.model_dump_json().encode("utf-8")) < 20_000
    assert "history-message" not in stored.event.model_dump_json()


def test_owner_bootstrap_is_one_time_and_cache_warming_is_economic(system):
    kernel, ledger, _, _ = system
    grant = kernel.owner_bootstrap.issue(
        base_url="https://nanihold.local",
        lifetime_seconds=60,
        idempotency_key="bootstrap:issue",
    )
    session = kernel.owner_bootstrap.exchange(
        code=grant.code,
        device_id="browser-owner",
        session_lifetime_seconds=3600,
        idempotency_key="bootstrap:exchange",
    )
    assert kernel.owner_bootstrap.authenticate(session.session_token) == "browser-owner"
    with pytest.raises(InvariantViolation, match="already used"):
        kernel.owner_bootstrap.exchange(
            code=grant.code,
            device_id="other",
            session_lifetime_seconds=3600,
            idempotency_key="bootstrap:reuse",
        )
    event_json = "".join(
        stored.event.model_dump_json() for stored in ledger.page(0, 100)
    )
    assert grant.code not in event_json
    assert session.session_token not in event_json

    policy = PilotPolicy(
        mode=PilotMode.MANAGED_PERMISSIONS,
        sandbox_profile=None,
        permission_classifier_enabled=True,
        writes_allowed=True,
    )
    adapter = ClaudePilotAdapter(adapter_version="1", policy=policy)
    root = ProviderSession(
        provider_session_id="provider-root",
        root_session_id="provider-root",
        relation="root",
        model_candidate_key="candidate:interface",
        working_directory_fingerprint="cwd:one",
        mcp_prefix_fingerprint="mcp:one",
    )
    opportunity = CacheOpportunity(
        root_session=root,
        requested_candidate_key="candidate:interface",
        working_directory_fingerprint="cwd:one",
        mcp_prefix_fingerprint="mcp:one",
        next_use_probability=0.8,
        posterior_confidence=0.95,
        cold_input_cost=100,
        cache_hit_input_cost=10,
        warming_cost=20,
        quota_remaining_fraction=0.5,
        quota_floor_fraction=0.2,
        owner_turn_queued=False,
    )
    assert adapter.decide_cache_warming(opportunity).warm
    launch = adapter.build_cache_warm_launch(opportunity)
    assert launch.argv[:3] == ("claude", "--resume", "provider-root")
    assert "--fork-session" in launch.argv
    assert ("CLAUDE_CODE_DISABLE_AUTO_MODEL_SWITCH", "1") in launch.env
    assert not adapter.decide_cache_warming(
        opportunity.model_copy(update={"owner_turn_queued": True})
    ).warm
