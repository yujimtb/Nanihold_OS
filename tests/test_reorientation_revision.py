from __future__ import annotations

import hashlib

import pytest

from conftest import NOW, OWNER_ID, SPACE_ID
from vsm.activation.models import (
    ActivationState,
    EvidenceCitation,
    HistoryImportReceipt,
    HistorySourceKind,
    HistorySourceManifest,
    ReorientationAssessment,
    ReorientationInterruptionReason,
    ReorientationRevisionReason,
)
from vsm.errors import InvariantViolation
from vsm.interface.service import InterfaceService
from vsm.kernel.service import Kernel
from vsm.projection import OperationalProjection
from vsm.runtime import _interrupt_reorientation_lost_on_runtime_restart
from vsm.token_lab.lab import TokenEfficiencyLab, TokenLabEventService


def _receipt() -> HistoryImportReceipt:
    sources = tuple(
        HistorySourceManifest(
            source_id=f"{kind.value}:revision-test",
            source_kind=kind,
            ownership="personal",
            owner_id=OWNER_ID,
            record_count=0,
            raw_bytes=0,
            digest_sha256=hashlib.sha256(kind.value.encode("utf-8")).hexdigest(),
            cutover_cursor=f"cursor:{kind.value}:revision-test",
        )
        for kind in HistorySourceKind
    )
    return HistoryImportReceipt(
        schema="schema:history-activation-handoff",
        schema_version="1.0.0",
        inventory_id="history-import:revision-test",
        data_space_id=SPACE_ID,
        manifest_digest=hashlib.sha256(b"revision-test").hexdigest(),
        record_count=0,
        raw_bytes=0,
        cross_source_overlap_identities=0,
        sources=sources,
        session_count=0,
        sessions=(),
        session_index_ref="history-index:revision-test",
        open_commitments_ref="history-commitments:revision-test",
        current_state_ref="history-state:revision-test",
    )


def _assessment(*, resume_work_item_ids: tuple[str, ...]) -> ReorientationAssessment:
    return ReorientationAssessment(
        assessment_id="assessment:revision-test",
        import_id="history-import:revision-test",
        conversation_id="conversation:revision-test",
        generated_at=NOW,
        understanding="Resume the current verified WorkItem.",
        active_missions=(),
        decisions_and_constraints=(),
        open_commitment_ids=(),
        unknowns=(),
        resume_work_item_ids=resume_work_item_ids,
        covered_session_index_ref="history-index:revision-test",
        covered_session_count=0,
        history_cursor=1,
        current_state_cursor=1,
        citations=(
            EvidenceCitation(
                claim_ref="understanding",
                evidence_ref="event:revision-evidence",
            ),
        ),
    )


def _prepare_assessment_gate(kernel: Kernel) -> None:
    kernel.activation.state = ActivationState.REORIENTATION_ONLY
    kernel.activation.reorientation_attempt_in_progress = True
    kernel.activation.import_receipt = _receipt()
    kernel.activation.reorientation_conversation_id = "conversation:revision-test"
    kernel.activation.history_query_operations.add("get_current_state")
    kernel.activation.history_query_event_ids.add("event:revision-evidence")


def test_assessment_requires_resume_target_when_current_work_exists(system):
    kernel, ledger, _, _ = system
    _prepare_assessment_gate(kernel)

    with pytest.raises(
        InvariantViolation,
        match="select at least one current WorkItem",
    ):
        kernel.activation.submit_assessment(
            _assessment(resume_work_item_ids=()),
            open_commitment_ids=(),
            existing_work_item_ids=("work:current",),
            session_index_listed_to_end=True,
            actor_id="pilot:interface",
            idempotency_key="assessment:missing-resume",
        )

    assert kernel.activation.state is ActivationState.REORIENTATION_ONLY
    assert kernel.activation.assessment is None
    assert all(
        stored.event.event_type != "reorientation_assessment_accepted"
        for stored in ledger.page(0, 100)
    )


def test_revision_projection_rebuild_clears_assessment_and_preserves_checkpoint_usage(
    system,
):
    kernel, ledger, _, pilot = system
    activation = kernel.activation
    activation.state = ActivationState.REORIENTATION_ONLY
    activation.reorientation_attempt_in_progress = True
    activation.record_reorientation_session_checkpoint(
        provider_session_id="provider:revision-checkpoint",
        actor_id="pilot:interface",
        idempotency_key="revision:checkpoint",
    )
    activation.record_reorientation_usage(
        input_tokens=120,
        output_tokens=30,
        actor_id="pilot:interface",
        idempotency_key="revision:usage",
    )
    activation.record_reorientation_failure(
        error_code="prior-error",
        actor_id="system:reorientation",
        idempotency_key="revision:failure",
    )
    assessment = _assessment(resume_work_item_ids=("work:current",))
    activation._record(
        event_type="reorientation_assessment_accepted",
        payload={
            "assessment": assessment.model_dump(mode="json"),
            "state": ActivationState.AWAITING_OWNER_CONFIRMATION,
        },
        actor_type="pilot",
        actor_id="pilot:interface",
        idempotency_key="revision:assessment",
    )
    activation.assessment = assessment
    activation.state = ActivationState.AWAITING_OWNER_CONFIRMATION

    activation.request_assessment_revision(
        ReorientationRevisionReason.MISSING_RESUME_WORK_ITEM,
        requested_by="system",
        actor_id="system:reorientation-gate",
        idempotency_key="revision:request",
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
        pilot=pilot,
        token_lab_events=rebuilt_token_events,
        clock=kernel.clock,
    )
    OperationalProjection(
        kernel=rebuilt_kernel,
        interface=rebuilt_interface,
    ).rebuild(page_size=2)

    rebuilt = rebuilt_kernel.activation
    assert rebuilt.state is ActivationState.REORIENTATION_ONLY
    assert rebuilt.assessment is None
    assert rebuilt.reorientation_error is None
    assert rebuilt.reorientation_provider_session_id == ("provider:revision-checkpoint")
    assert rebuilt.reorientation_pilot_calls == 1
    assert rebuilt.reorientation_input_tokens == 120
    assert rebuilt.reorientation_output_tokens == 30
    assert rebuilt.reorientation_attempt_in_progress is False
    assert (
        rebuilt.pending_reorientation_revision_reason
        is ReorientationRevisionReason.MISSING_RESUME_WORK_ITEM
    )
    assert rebuilt_kernel.executions == {}
    assert rebuilt_kernel.effect_leases == {}
    revision_event = ledger.page(0, 100)[-1].event
    assert revision_event.actor_type == "system"
    assert revision_event.payload == {
        "prior_assessment_id": "assessment:revision-test",
        "reason_code": "missing_resume_work_item",
        "state": "REORIENTATION_ONLY",
    }

    rebuilt.start_reorientation(
        actor_id="pilot:interface",
        idempotency_key="revision:retry",
    )

    assert rebuilt.state is ActivationState.REORIENTATION_ONLY
    assert rebuilt.reorientation_attempt_in_progress is True
    assert rebuilt.pending_reorientation_revision_reason is None
    assert rebuilt.reorientation_provider_session_id == ("provider:revision-checkpoint")
    assert rebuilt.reorientation_pilot_calls == 1
    revision_retry_event = ledger.page(0, 100)[-1].event
    assert revision_retry_event.event_type == ("reorientation_revision_retry_started")
    assert revision_retry_event.payload == {
        "state": "REORIENTATION_ONLY",
        "retry_reason": "assessment_revision",
        "revision_reason_code": "missing_resume_work_item",
    }


def test_in_progress_attempt_rejects_second_start_and_rebuild_preserves_lock(system):
    kernel, ledger, _, pilot = system
    activation = kernel.activation
    activation.state = ActivationState.HISTORY_IMPORTED
    activation.start_reorientation(
        actor_id="pilot:interface",
        idempotency_key="attempt:start",
    )

    with pytest.raises(
        InvariantViolation,
        match="already in progress",
    ):
        activation.start_reorientation(
            actor_id="pilot:other",
            idempotency_key="attempt:different-start",
        )

    rebuilt_kernel = Kernel(
        data_space=kernel.data_space,
        ledger=ledger,
        audit_policy=kernel.audit_policy,
        control_policy=kernel.control_policy,
        clock=kernel.clock,
    )
    rebuilt_interface = InterfaceService(
        kernel=rebuilt_kernel,
        ledger=ledger,
        pilot=pilot,
        token_lab_events=TokenLabEventService(
            lab=TokenEfficiencyLab(),
            ledger=ledger,
            data_space_id=SPACE_ID,
            clock=kernel.clock,
        ),
        clock=kernel.clock,
    )
    OperationalProjection(
        kernel=rebuilt_kernel,
        interface=rebuilt_interface,
    ).rebuild(page_size=1)

    assert rebuilt_kernel.activation.reorientation_attempt_in_progress is True
    with pytest.raises(
        InvariantViolation,
        match="already in progress",
    ):
        rebuilt_kernel.activation.start_reorientation(
            actor_id="pilot:interface",
            idempotency_key="attempt:after-rebuild",
        )
    start_events = [
        item.event
        for item in ledger.page(0, 100)
        if item.event.event_type == "reorientation_started"
    ]
    assert len(start_events) == 1


def test_rebuilt_open_attempt_requires_explicit_failure_before_retry(system):
    kernel, ledger, _, pilot = system
    kernel.activation.state = ActivationState.HISTORY_IMPORTED
    kernel.activation.start_reorientation(
        actor_id="pilot:interface",
        idempotency_key="interrupted:start",
    )

    rebuilt_kernel = Kernel(
        data_space=kernel.data_space,
        ledger=ledger,
        audit_policy=kernel.audit_policy,
        control_policy=kernel.control_policy,
        clock=kernel.clock,
    )
    rebuilt_interface = InterfaceService(
        kernel=rebuilt_kernel,
        ledger=ledger,
        pilot=pilot,
        token_lab_events=TokenLabEventService(
            lab=TokenEfficiencyLab(),
            ledger=ledger,
            data_space_id=SPACE_ID,
            clock=kernel.clock,
        ),
        clock=kernel.clock,
    )
    OperationalProjection(
        kernel=rebuilt_kernel,
        interface=rebuilt_interface,
    ).rebuild()

    rebuilt = rebuilt_kernel.activation
    assert rebuilt.reorientation_attempt_in_progress is True
    rebuilt.interrupt_reorientation_attempt(
        reason_code=(
            ReorientationInterruptionReason.PROCESS_RESTART_INTERRUPTED_ATTEMPT
        ),
        actor_id="system:reorientation-recovery",
        idempotency_key="interrupted:explicit-recovery",
    )
    assert rebuilt.reorientation_attempt_in_progress is False
    assert rebuilt.reorientation_error == "process_restart_interrupted_attempt"

    rebuilt.start_reorientation(
        actor_id="pilot:interface",
        idempotency_key="interrupted:retry",
    )
    assert rebuilt.reorientation_attempt_in_progress is True
    assert rebuilt.reorientation_error is None
    events = [item.event for item in ledger.page(0, 100)]
    assert events[-2].event_type == "reorientation_attempt_interrupted"
    assert events[-2].payload == {"reason_code": "process_restart_interrupted_attempt"}
    retry_event = events[-1]
    assert retry_event.event_type == "reorientation_retry_started"
    assert retry_event.payload == {
        "state": "REORIENTATION_ONLY",
        "retry_reason": "failure",
        "prior_error_code": "process_restart_interrupted_attempt",
    }


def test_runtime_bootstrap_records_lost_attempt_interruption_once(system):
    kernel, ledger, _, pilot = system
    kernel.activation.state = ActivationState.HISTORY_IMPORTED
    kernel.activation.start_reorientation(
        actor_id="pilot:interface",
        idempotency_key="runtime-recovery:start",
    )
    start_version = kernel.activation.reorientation_attempt_started_stream_version
    assert start_version is not None

    rebuilt_kernel = Kernel(
        data_space=kernel.data_space,
        ledger=ledger,
        audit_policy=kernel.audit_policy,
        control_policy=kernel.control_policy,
        clock=kernel.clock,
    )
    rebuilt_interface = InterfaceService(
        kernel=rebuilt_kernel,
        ledger=ledger,
        pilot=pilot,
        token_lab_events=TokenLabEventService(
            lab=TokenEfficiencyLab(),
            ledger=ledger,
            data_space_id=SPACE_ID,
            clock=kernel.clock,
        ),
        clock=kernel.clock,
    )
    OperationalProjection(
        kernel=rebuilt_kernel,
        interface=rebuilt_interface,
    ).rebuild()

    assert _interrupt_reorientation_lost_on_runtime_restart(rebuilt_kernel)
    assert not _interrupt_reorientation_lost_on_runtime_restart(rebuilt_kernel)
    status = rebuilt_kernel.activation.status()
    assert status.reorientation_attempt_in_progress is False
    assert status.reorientation_error == ("process_restart_interrupted_attempt")
    events = [item.event for item in ledger.page(0, 100)]
    interruptions = [
        event
        for event in events
        if event.event_type == "reorientation_attempt_interrupted"
    ]
    assert len(interruptions) == 1
    assert interruptions[0].idempotency_key == (
        f"reorientation:runtime-restart-interruption:{start_version}"
    )
