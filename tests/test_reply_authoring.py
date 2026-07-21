from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vsm.reply_authoring import (
    REPLY_DRAFT_KIND,
    ReplyDraftSubmission,
    submit_reply_draft,
)


class RecordingSupplementalGateway:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def write_supplemental(self, **record: object) -> dict[str, object]:
        self.records.append(record)
        return {"record": record}


def draft() -> ReplyDraftSubmission:
    return ReplyDraftSubmission.new(
        incoming_observation_id="obs:incoming-message",
        channel="slack",
        recipient="U123",
        body="確認しました。明日までに対応します。",
        drafted_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        agent_name="Aki",
        work_item_id="work:reply-authoring",
        execution_id="execution:reply-authoring",
        thread_ref="thread:incoming-message",
    )


def test_reply_draft_is_attributed_and_anchored_without_approval_or_send():
    submission = draft()

    envelope = submission.supplemental()

    assert envelope["id"] == submission.draft_id
    assert envelope["kind"] == REPLY_DRAFT_KIND
    assert envelope["derived_from"] == {
        "observations": ["obs:incoming-message"],
        "blobs": [],
        "supplementals": [],
    }
    assert envelope["payload"] == {
        "channel": "slack",
        "recipient": "U123",
        "body": "確認しました。明日までに対応します。",
        "drafted_at": "2026-07-21T12:00:00Z",
        "thread_ref": "thread:incoming-message",
    }
    assert envelope["created_by"] == "agent:Aki"
    assert envelope["lineage"] == (
        "nanihold/work-item/work:reply-authoring/execution/"
        "execution:reply-authoring/agent/Aki"
    )
    assert "reply-approval@1" not in envelope
    assert "send-record@1" not in envelope


def test_reply_draft_requires_explicit_body():
    with pytest.raises(ValidationError):
        ReplyDraftSubmission.new(
            incoming_observation_id="obs:incoming-message",
            channel="slack",
            recipient="U123",
            body="",
            drafted_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
            agent_name="Aki",
            work_item_id="work:reply-authoring",
            execution_id="execution:reply-authoring",
        )


def test_reply_draft_requires_aware_timestamp():
    with pytest.raises(ValidationError):
        ReplyDraftSubmission.new(
            incoming_observation_id="obs:incoming-message",
            channel="slack",
            recipient="U123",
            body="明示的な本文",
            drafted_at=datetime(2026, 7, 21, 12, 0),
            agent_name="Aki",
            work_item_id="work:reply-authoring",
            execution_id="execution:reply-authoring",
        )


def test_submit_reply_draft_uses_existing_gateway_without_approval_or_send():
    gateway = RecordingSupplementalGateway()
    submission = draft()

    response = submit_reply_draft(gateway, submission)

    assert response["record"] == gateway.records[0]
    assert gateway.records == [submission.supplemental()]
    assert gateway.records[0]["kind"] == "reply-draft@1"
    assert "reply-approval@1" not in gateway.records[0]
    assert "send-record@1" not in gateway.records[0]


def test_send_record_trace_keeps_draft_attribution_through_draft_anchor():
    submission = draft()
    draft_record = submission.supplemental()
    send_record = {
        "kind": "send-record@1",
        "derived_from": {
            "observations": [],
            "blobs": [],
            "supplementals": [draft_record["id"]],
        },
        "payload": {"channel": "slack", "mode": "approved"},
    }

    assert send_record["derived_from"]["supplementals"] == [submission.draft_id]
    assert draft_record["created_by"] == "agent:Aki"
    assert "work:reply-authoring" in draft_record["lineage"]
    assert "execution:reply-authoring" in draft_record["lineage"]
