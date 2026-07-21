from __future__ import annotations

import pytest

from vsm.acr08 import (
    EvidenceVerificationError,
    automation_plan,
    build_matrix,
    matrix_manifest,
    render_owner_checklist,
    resolve_scope_argument,
    verify_results,
)


def _evidence_and_results() -> tuple[dict[str, object], dict[str, object]]:
    results: list[dict[str, object]] = []
    ledger_events: list[dict[str, object]] = []
    audit_traces: dict[str, dict[str, object]] = {}
    observation_subjects: list[str] = []

    for cell in build_matrix():
        if not cell["applicable"]:
            continue
        cell_id = str(cell["cell_id"])
        if cell["direction"] == "agent_to_owner":
            draft_id = f"sup:draft-{cell_id.lower()}"
            approval_id = f"sup:approval-{cell_id.lower()}"
            send_id = f"sup:send-{cell_id.lower()}"
            audit_traces[cell_id] = {
                "trace_kind": "reply_delivery",
                "verified": True,
                "draft": {"id": draft_id},
                "approval": {"id": approval_id},
                "delivery": {"kind": "send-record@1", "id": send_id},
            }
            results.append(
                {
                    "cell_id": cell_id,
                    "status": "passed",
                    "channel": cell["channel"],
                    "audit_trace_subject": cell_id,
                    "draft_id": draft_id,
                    "approval_id": approval_id,
                    "send_record_id": send_id,
                    "external_send_performed": False,
                }
            )
            continue

        if cell["resolution"] == "observation_only":
            subject = f"message:{cell_id.lower()}"
            observation_subjects.append(subject)
            results.append(
                {
                    "cell_id": cell_id,
                    "status": "passed",
                    "channel": cell["channel"],
                    "observation_subject": subject,
                }
            )
            continue

        notification_id = f"notification:{cell_id.lower()}"
        event_id = f"event:{cell_id.lower()}"
        source_message_id = f"message:{cell_id.lower()}"
        ledger_events.append(
            {
                "cursor": len(ledger_events) + 1,
                "event": {
                    "event_id": event_id,
                    "event_type": "agent_notification_delivered",
                    "payload": {
                        "notification": {
                            "notification_id": notification_id,
                            "source_message_id": source_message_id,
                            "source_observation_subject": f"observation:{cell_id.lower()}",
                        }
                    },
                },
            }
        )
        audit_traces[cell_id] = {
            "trace_kind": "notification_delivery",
            "verified": True,
            "recipient_agent_name": "Nagi"
            if cell["expected_target"] == "Nagi"
            else "Toki",
            "incoming": {"source_message_id": source_message_id},
            "delivery": {"ledger_event_id": event_id},
        }
        results.append(
            {
                "cell_id": cell_id,
                "status": "passed",
                "channel": cell["channel"],
                "notification_id": notification_id,
                "source_message_id": source_message_id,
                "audit_trace_subject": cell_id,
            }
        )

    return (
        {
            "matrix_version": "acr-08/v1",
            "real_external_sends_performed": False,
            "cells": results,
        },
        {
            "matrix_version": "acr-08/v1",
            "ledger_events": ledger_events,
            "audit_traces": audit_traces,
            "observation_subjects": observation_subjects,
            "allow_external_send": False,
        },
    )


def test_matrix_is_the_60_cell_product_with_26_applicable_cells() -> None:
    manifest = matrix_manifest()
    assert manifest["counts"] == {
        "total": 60,
        "applicable": 26,
        "na": 34,
        "owner_checklist": 20,
        "automated_dry_run": 6,
    }
    assert len(build_matrix()) == 60
    assert all(
        cell["na_reason"] for cell in build_matrix() if not cell["applicable"]
    )


def test_dry_run_has_six_internal_requests_and_no_external_send() -> None:
    plan = automation_plan()
    assert plan["dry_run"] is True
    assert plan["real_discord_or_slack_send"] is False
    requests = plan["requests"]
    assert isinstance(requests, list)
    assert len(requests) == 6
    assert all(item["external_send"] is False for item in requests)
    assert sum(item["request"]["method"] == "POST" for item in requests) == 4
    assert sum(item["request"]["method"] == "GET" for item in requests) == 2


def test_owner_checklist_contains_all_20_operational_cells() -> None:
    checklist = render_owner_checklist()
    assert checklist.count("### ") == 20
    assert "送る文言" in checklist
    assert "reply-approval@1" in checklist
    assert "ACR08-DISCORD-OBSERVATION_ONLY-NAGI-OWNER_TO_AGENT" in checklist


def test_results_are_verified_against_ledger_and_audit_traces() -> None:
    results, evidence = _evidence_and_results()
    verified = verify_results(results, evidence)
    assert verified["verified"] is True
    assert verified["verified_count"] == 26
    assert verified["na_count"] == 34


def test_verifier_rejects_missing_ledger_evidence() -> None:
    results, evidence = _evidence_and_results()
    evidence["ledger_events"] = []
    with pytest.raises(EvidenceVerificationError, match="Ledger has no matching"):
        verify_results(results, evidence)


def test_verifier_rejects_external_send_without_explicit_opt_in() -> None:
    results, evidence = _evidence_and_results()
    results["real_external_sends_performed"] = True
    with pytest.raises(EvidenceVerificationError, match="external sends"):
        verify_results(results, evidence)


def _first_notification_cell_id(direction: str) -> str:
    for cell in build_matrix():
        if (
            cell["applicable"]
            and cell["direction"] == direction
            and cell["resolution"] != "observation_only"
        ):
            return str(cell["cell_id"])
    raise AssertionError(f"no applicable notification cell for direction={direction}")


def test_verifier_accepts_internal_source_platform_for_agent_to_agent_cell() -> None:
    """AgentNotificationDelivery.send_agent_message always records
    source_platform="internal" for agent_to_agent notifications (see
    vsm/notifications.py), and AuditTraceService.trace_notification always
    returns that key (see vsm/audit_trace.py).  Real automated-cell evidence
    therefore carries an explicit source_platform="internal", and the
    verifier must accept it for the internal-path (agent_to_agent) cells.
    """

    results, evidence = _evidence_and_results()
    cell_id = _first_notification_cell_id("agent_to_agent")
    evidence["audit_traces"][cell_id]["incoming"]["source_platform"] = "internal"

    verified = verify_results(results, evidence)

    assert verified["verified"] is True
    assert cell_id in verified["cell_ids"]


def test_verifier_rejects_internal_source_platform_for_owner_to_agent_cell() -> None:
    """owner_to_agent cells are real channel-inbound notifications, so an
    incoming source_platform of "internal" would mean the evidence does not
    actually prove delivery over the claimed Discord/Slack channel; the
    verifier must keep rejecting that for non-agent_to_agent cells.
    """

    results, evidence = _evidence_and_results()
    cell_id = _first_notification_cell_id("owner_to_agent")
    evidence["audit_traces"][cell_id]["incoming"]["source_platform"] = "internal"

    with pytest.raises(EvidenceVerificationError, match="audit trace channel does not match result"):
        verify_results(results, evidence)


def _automated_cell_ids() -> set[str]:
    return {
        str(cell["cell_id"])
        for cell in build_matrix()
        if cell["execution_mode"] == "automated_dry_run"
    }


def _owner_cell_ids() -> set[str]:
    return {
        str(cell["cell_id"])
        for cell in build_matrix()
        if cell["execution_mode"] == "owner_checklist"
    }


def test_scope_defaults_to_full_and_preserves_legacy_behaviour() -> None:
    """scope=None must keep the original all-26-cells, all-or-nothing
    behaviour and the original verified_count/na_count values byte-for-byte,
    with only a new "scope": "full" key added to the response."""

    results, evidence = _evidence_and_results()
    verified = verify_results(results, evidence)
    assert verified["scope"] == "full"
    assert verified["verified_count"] == 26
    assert verified["na_count"] == 34
    assert set(verified["cell_ids"]) == _automated_cell_ids() | _owner_cell_ids()


def test_scope_automated_only_verifies_the_six_automated_cells() -> None:
    """The 6 automated_dry_run cells are already measured; scope=automated
    must allow verifying just those without supplying the 20 owner cells."""

    results, evidence = _evidence_and_results()
    automated_ids = _automated_cell_ids()
    assert len(automated_ids) == 6
    results["cells"] = [
        cell for cell in results["cells"] if cell["cell_id"] in automated_ids
    ]

    verified = verify_results(results, evidence, scope=automated_ids)

    assert verified["verified"] is True
    assert verified["scope"] == sorted(automated_ids)
    assert verified["verified_count"] == 6
    assert verified["na_count"] == 34
    assert set(verified["cell_ids"]) == automated_ids


def test_scope_owner_only_verifies_the_twenty_owner_cells() -> None:
    """Symmetric case: scope=owner must allow verifying just the 20
    owner-executed cells (e.g. once the Lake-injection simulation lands),
    independently of the 6 automated cells."""

    results, evidence = _evidence_and_results()
    owner_ids = _owner_cell_ids()
    assert len(owner_ids) == 20
    results["cells"] = [
        cell for cell in results["cells"] if cell["cell_id"] in owner_ids
    ]

    verified = verify_results(results, evidence, scope=owner_ids)

    assert verified["verified"] is True
    assert verified["scope"] == sorted(owner_ids)
    assert verified["verified_count"] == 20
    assert set(verified["cell_ids"]) == owner_ids


def test_scope_rejects_results_containing_a_cell_outside_scope() -> None:
    """If results include a cell that is not in the requested scope, that is
    an over- (or under-) delivery relative to what was asked to be verified
    and must be rejected rather than silently accepted or ignored."""

    results, evidence = _evidence_and_results()
    automated_ids = _automated_cell_ids()
    owner_extra = next(iter(_owner_cell_ids()))
    results["cells"] = [
        cell
        for cell in results["cells"]
        if cell["cell_id"] in automated_ids or cell["cell_id"] == owner_extra
    ]

    with pytest.raises(EvidenceVerificationError, match="scoped cells"):
        verify_results(results, evidence, scope=automated_ids)


def test_scope_rejects_non_applicable_na_cell_id() -> None:
    """N/A cells have no verification points and can never be proven by
    evidence; a scope naming one is an error, not a silently-skipped cell."""

    results, evidence = _evidence_and_results()
    na_cell_id = next(
        str(cell["cell_id"]) for cell in build_matrix() if not cell["applicable"]
    )

    with pytest.raises(EvidenceVerificationError, match="non-applicable"):
        verify_results(results, evidence, scope=_automated_cell_ids() | {na_cell_id})


def test_scope_rejects_unknown_cell_id() -> None:
    results, evidence = _evidence_and_results()

    with pytest.raises(EvidenceVerificationError, match="unknown cell_id"):
        verify_results(results, evidence, scope={"ACR08-NOT-A-REAL-CELL"})


def test_scope_rejects_empty_scope() -> None:
    results, evidence = _evidence_and_results()

    with pytest.raises(EvidenceVerificationError, match="scope must not be empty"):
        verify_results(results, evidence, scope=set())


def test_resolve_scope_argument_literals_and_csv() -> None:
    assert set(resolve_scope_argument("automated")) == _automated_cell_ids()
    assert set(resolve_scope_argument("owner")) == _owner_cell_ids()
    two_ids = sorted(_automated_cell_ids())[:2]
    assert resolve_scope_argument(",".join(two_ids)) == tuple(two_ids)
