from datetime import UTC, datetime

import httpx
import pytest

from vsm.activation.models import (
    ActivationState,
    ActivationStatus,
    EvidenceCitation,
    HistoryImportReceipt,
    HistorySession,
    HistorySourceKind,
    HistorySourceManifest,
    ReorientationAssessment,
)
from vsm.errors import InvariantViolation
from vsm.tui import (
    TuiOperationalSnapshot,
    load_operational_snapshot,
    render_dashboard,
)


def _receipt() -> HistoryImportReceipt:
    source_counts = {
        HistorySourceKind.CLAUDE_CODE: (119, 4000),
        HistorySourceKind.INTERCOM: (149, 8000),
    }
    sources = tuple(
        HistorySourceManifest(
            source_id=f"source:{kind.value}",
            source_kind=kind,
            ownership="personal",
            owner_id="human:owner",
            record_count=source_counts.get(kind, (0, 0))[0],
            raw_bytes=source_counts.get(kind, (0, 0))[1],
            digest_sha256=f"{index + 1:064x}",
            cutover_cursor=f"{kind.value}:cursor",
        )
        for index, kind in enumerate(HistorySourceKind)
    )
    return HistoryImportReceipt(
        schema="schema:history-activation-handoff",
        schema_version="1.0.0",
        inventory_id="import:owner-history",
        data_space_id="data-space:personal",
        manifest_digest="a" * 64,
        record_count=268,
        raw_bytes=12000,
        cross_source_overlap_identities=0,
        sources=sources,
        session_count=2,
        sessions=(
            HistorySession(
                session_ref="history-session:claude",
                source_session_id="claude-root",
                source_kind=HistorySourceKind.CLAUDE_CODE,
                source_id="source:claude_code",
                message_count=119,
                first_message_at=datetime(2026, 7, 19, tzinfo=UTC),
                last_message_at=datetime(2026, 7, 20, tzinfo=UTC),
            ),
            HistorySession(
                session_ref="history-session:intercom",
                source_session_id="intercom-owner",
                source_kind=HistorySourceKind.INTERCOM,
                source_id="source:intercom",
                message_count=149,
                first_message_at=datetime(2026, 7, 19, tzinfo=UTC),
                last_message_at=datetime(2026, 7, 20, tzinfo=UTC),
            ),
        ),
        session_index_ref="blob:session-index",
        open_commitments_ref="blob:open-commitments",
        current_state_ref="blob:current-state",
    )


def _assessment() -> ReorientationAssessment:
    return ReorientationAssessment(
        assessment_id="assessment:first",
        import_id="import:owner-history",
        conversation_id="conversation:owner-main",
        generated_at=datetime(2026, 7, 20, tzinfo=UTC),
        understanding="Naniholdの完全稼働とInterface再起動を進めています。",
        active_missions=("Interface UXを回復する",),
        decisions_and_constraints=("初回だけowner確認を要求する",),
        open_commitment_ids=("commitment:one",),
        unknowns=("production quotaの実測値",),
        resume_work_item_ids=("work-item:resume",),
        covered_session_index_ref="history-projection:sessions:sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        covered_session_count=2,
        history_cursor=220,
        current_state_cursor=224,
        citations=(
            EvidenceCitation(
                claim_ref="claim:mission",
                evidence_ref="message:owner-plan",
            ),
        ),
    )


def _assessment_without_resume_work() -> ReorientationAssessment:
    return _assessment().model_copy(update={"resume_work_item_ids": ()})


def test_render_dashboard_is_deterministic_and_owner_centered() -> None:
    status = ActivationStatus(
        state=ActivationState.AWAITING_OWNER_CONFIRMATION,
        import_receipt=_receipt(),
        assessment=_assessment(),
        approved_at=None,
        status_model_calls=0,
        reorientation_pilot_calls=1,
        reorientation_input_tokens=1200,
        reorientation_output_tokens=300,
        reorientation_error=None,
        work_graph_snapshot_id="work-graph:cutover",
    )
    snapshot = TuiOperationalSnapshot(
        activation=status,
        current_work=("Nanihold backend統合",),
        waiting_work=("owner confirmation",),
        delegations=("frontend → Interface Node",),
        cost_usd=0.125,
        quota="17% / reset 2h03m",
        evidence_refs=("message:owner-plan", "event:history-import"),
    )

    rendered = render_dashboard(snapshot, width=88)

    assert rendered == render_dashboard(snapshot, width=88)
    assert "AWAITING_OWNER_CONFIRMATION — owner確認待ち" in rendered
    assert "履歴: 268 records / 2 sources" in rendered
    assert "Interfaceが追いつきました" in rendered
    assert "Naniholdの完全稼働とInterface再起動" in rendered
    assert "次の操作:" in rendered
    assert "Bearer" not in rendered
    assert "\x1b" not in rendered


def test_dashboard_blocks_approval_when_resume_work_is_empty() -> None:
    status = ActivationStatus(
        state=ActivationState.AWAITING_OWNER_CONFIRMATION,
        import_receipt=_receipt(),
        assessment=_assessment_without_resume_work(),
        approved_at=None,
        status_model_calls=0,
        reorientation_pilot_calls=1,
        reorientation_input_tokens=1200,
        reorientation_output_tokens=300,
        reorientation_error=None,
        work_graph_snapshot_id="work-graph:cutover",
    )

    rendered = render_dashboard(
        TuiOperationalSnapshot(
            activation=status,
            current_work=(),
            waiting_work=(),
            delegations=(),
            cost_usd=None,
            quota=None,
            evidence_refs=(),
        ),
        width=88,
    )

    assert "このassessmentは承認できません" in rendered
    assert "vsm reorientation revise" in rendered
    assert "ExecutionとEffectは開始されていません" in rendered


def test_pre_activation_dashboard_states_hard_gate() -> None:
    status = ActivationStatus(
        state=ActivationState.UNCOMMISSIONED,
        import_receipt=None,
        assessment=None,
        approved_at=None,
        status_model_calls=0,
        reorientation_pilot_calls=0,
        reorientation_input_tokens=0,
        reorientation_output_tokens=0,
        reorientation_error=None,
        work_graph_snapshot_id=None,
    )
    rendered = render_dashboard(
        TuiOperationalSnapshot(
            activation=status,
            current_work=(),
            waiting_work=(),
            delegations=(),
            cost_usd=None,
            quota=None,
            evidence_refs=(),
        )
    )

    assert "UNCOMMISSIONED — 履歴取込待ち" in rendered
    assert "安全ゲート: owner承認までExecutionとEffectは開始されません。" in rendered
    assert "現在: —" in rendered
    assert "費用・quota: 未計測 / telemetry待ち" in rendered


@pytest.mark.parametrize("width", [0, 40, 59])
def test_tui_rejects_unusable_width(width: int) -> None:
    status = ActivationStatus(
        state=ActivationState.ACTIVE,
        import_receipt=_receipt(),
        assessment=_assessment(),
        approved_at=datetime(2026, 7, 20, tzinfo=UTC),
        status_model_calls=0,
        reorientation_pilot_calls=1,
        reorientation_input_tokens=1,
        reorientation_output_tokens=1,
        reorientation_error=None,
        work_graph_snapshot_id="work-graph:cutover",
    )
    with pytest.raises(ValueError, match="at least 60"):
        render_dashboard(
            TuiOperationalSnapshot(
                activation=status,
                current_work=(),
                waiting_work=(),
                delegations=(),
                cost_usd=0,
                quota="100%",
                evidence_refs=(),
            ),
            width=width,
        )


def test_tui_rejects_negative_cost() -> None:
    status = ActivationStatus(
        state=ActivationState.ACTIVE,
        import_receipt=_receipt(),
        assessment=_assessment(),
        approved_at=datetime(2026, 7, 20, tzinfo=UTC),
        status_model_calls=0,
        reorientation_pilot_calls=1,
        reorientation_input_tokens=1,
        reorientation_output_tokens=1,
        reorientation_error=None,
        work_graph_snapshot_id="work-graph:cutover",
    )
    with pytest.raises(ValueError, match="non-negative"):
        TuiOperationalSnapshot(
            activation=status,
            current_work=(),
            waiting_work=(),
            delegations=(),
            cost_usd=-0.1,
            quota="100%",
            evidence_refs=(),
        )


def test_live_tui_reads_only_model_free_projections() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/api/activation/status":
            return httpx.Response(
                200,
                json=ActivationStatus(
                    state=ActivationState.AWAITING_OWNER_CONFIRMATION,
                    import_receipt=_receipt(),
                    assessment=_assessment(),
                    approved_at=None,
                    status_model_calls=0,
                    reorientation_pilot_calls=1,
                    reorientation_input_tokens=100,
                    reorientation_output_tokens=20,
                    reorientation_error=None,
                    work_graph_snapshot_id="work-graph:cutover",
                ).model_dump(mode="json"),
            )
        if request.url.path == "/api/work-items":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "work_item_id": "work:active",
                            "title": "履歴統合",
                            "state": "active",
                            "owner_node_id": "node:interface",
                            "delegated_to_node_id": "node:s1-history",
                        },
                        {
                            "work_item_id": "work:blocked",
                            "title": "owner確認",
                            "state": "blocked",
                            "owner_node_id": "node:interface",
                            "delegated_to_node_id": "node:interface",
                        },
                    ],
                    "edges": [],
                },
            )
        if request.url.path == "/api/executions":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "execution_id": "execution:history",
                            "state": "active",
                        }
                    ],
                    "effect_leases": [],
                    "budget_reservations": [],
                },
            )
        raise AssertionError(request.url)

    with httpx.Client(
        base_url="https://nanihold.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        snapshot = load_operational_snapshot(client)

    assert requested == [
        "/api/activation/status",
        "/api/work-items",
        "/api/executions",
    ]
    assert snapshot.current_work == (
        "履歴統合 [active]",
        "Execution execution:history",
    )
    assert snapshot.waiting_work == ("owner確認 [blocked]",)
    assert snapshot.delegations == ("履歴統合 → node:s1-history",)
    assert snapshot.evidence_refs == ("message:owner-plan",)
    assert snapshot.cost_usd is None
    assert snapshot.quota is None


def test_live_tui_fails_fast_on_projection_schema_drift() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/activation/status":
            return httpx.Response(
                200,
                json=ActivationStatus(
                    state=ActivationState.UNCOMMISSIONED,
                    import_receipt=None,
                    assessment=None,
                    approved_at=None,
                    status_model_calls=0,
                    reorientation_pilot_calls=0,
                    reorientation_input_tokens=0,
                    reorientation_output_tokens=0,
                    reorientation_error=None,
                    work_graph_snapshot_id=None,
                ).model_dump(mode="json"),
            )
        return httpx.Response(200, json={"items": "not-a-list"})

    with httpx.Client(
        base_url="https://nanihold.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(InvariantViolation, match="work-items.items"):
            load_operational_snapshot(client)
