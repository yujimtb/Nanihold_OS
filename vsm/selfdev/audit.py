"""S3★独立監査の strict adapter。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vsm.agents import AgentRequest, AgentRuntimeProtocol
from vsm.clock import Clock, SystemClock
from vsm.selfdev.artifacts import sha256_file
from vsm.selfdev.git import CandidateCommit
from vsm.selfdev.models import (
    AcceptanceResult,
    AuditBudget,
    AuditCandidate,
    AuditFinding,
    AuditInputs,
    AuditReport,
    Auditor,
    GateReport,
    ProposalManifest,
    ScopeCheck,
)
from vsm.selfdev.verification import ProtectedApproval, scope_sha256, verify_scope


class AuditError(RuntimeError):
    """監査の入力・protocol・証拠検証に失敗した。"""


class S3StarAuditRunner:
    """S1 と session を共有しない S3★監査実行器。"""

    def __init__(self, *, runtime: AgentRuntimeProtocol, clock: Clock | None = None) -> None:
        if runtime is None:
            raise ValueError("S3★ auditor runtime は必須です")
        session_ref = getattr(runtime, "session_ref", None)
        if not isinstance(session_ref, str) or not session_ref.strip():
            raise ValueError("S3★ auditor runtime には独立 session_ref が必要です")
        self.runtime = runtime
        self.clock = clock or SystemClock()

    @staticmethod
    def _parse_response(text: str, criteria: tuple[str, ...]) -> dict[str, Any]:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AuditError("audit runtime response は JSON object が必要です") from exc
        if not isinstance(raw, dict):
            raise AuditError("audit runtime response は object が必要です")
        results = raw.get("acceptance_results")
        if not isinstance(results, list) or len(results) != len(criteria):
            raise AuditError("acceptance_results は受入条件を全件一度ずつ必要とします")
        ids = [item.get("criterion_id") for item in results if isinstance(item, dict)]
        if ids != list(criteria):
            raise AuditError("acceptance_results の criterion 順序または id が不一致です")
        for item in results:
            if not isinstance(item, dict) or item.get("status") not in {"pass", "fail", "indeterminate"}:
                raise AuditError("acceptance_results の status が不正です")
        findings = raw.get("findings", [])
        if not isinstance(findings, list):
            raise AuditError("findings は配列が必要です")
        verdict = raw.get("verdict")
        if verdict not in {"pass", "fail", "indeterminate"}:
            raise AuditError("audit verdict が不正です")
        summary = raw.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise AuditError("audit summary は非空文字列が必要です")
        return raw

    async def run(
        self,
        *,
        proposal: ProposalManifest,
        candidate: CandidateCommit,
        gate_report: GateReport,
        root: Path,
        proposal_manifest_ref: str,
        manifest_path: Path,
        gate_report_ref: str,
        gate_report_path: Path,
        diff_ref: str,
        diff_path: Path,
        changed_paths: tuple[str, ...],
        protected_approval: ProtectedApproval | Mapping[str, Any] | None,
        budget_actual: Mapping[str, Any],
        audit_id: str,
    ) -> AuditReport:
        for path in (manifest_path, gate_report_path, diff_path):
            if not path.is_file():
                raise AuditError(f"audit input artifact がありません: {path}")
        if gate_report.proposal_id != proposal.id:
            raise AuditError("GateReport の Proposal id が不一致です")
        if gate_report.candidate_diff_sha256 != candidate.diff_sha256:
            raise AuditError("GateReport と candidate の diff digest が不一致です")
        expected_scope = verify_scope(
            changed_paths,
            [rule.model_dump(mode="json") for rule in proposal.scope],
            protected_approval=protected_approval,
            proposal_manifest_sha256=proposal.sha256(),
            protected_scope_sha256=scope_sha256([rule.model_dump(mode="json") for rule in proposal.scope]),
            risk_class=proposal.risk_class,
        )
        if expected_scope.outside_scope_paths:
            raise AuditError("audit input に scope 外 path があります")

        criteria = tuple(item.id for item in proposal.acceptance_criteria)
        prompt = (
            "S3★ independent self-development audit. S1 の申告を信頼せず、"
            "manifest、candidate diff、gate report、raw evidence を突合し、strict JSON を返す。\n"
            + json.dumps(
                {
                    "proposal": proposal.canonical_dict(),
                    "candidate": candidate.to_dict(),
                    "gate_report": gate_report.model_dump(mode="json"),
                    "changed_paths": list(changed_paths),
                    "acceptance_criteria": [item.model_dump(mode="json") for item in proposal.acceptance_criteria],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        result = await asyncio.wait_for(
            self.runtime.invoke(AgentRequest(prompt=prompt, context_view=None)),
            timeout=self.runtime.timeout_seconds,
        )
        raw = self._parse_response(result.text, criteria)

        acceptance_results = tuple(
            AcceptanceResult(
                criterion_id=item["criterion_id"],
                status=item["status"],
                evidence_refs=tuple(item.get("evidence_refs", [])),
                finding=str(item.get("finding", "")),
            )
            for item in raw["acceptance_results"]
        )
        findings = tuple(
            AuditFinding(
                finding_id=str(item["finding_id"]),
                severity=item["severity"],
                category=str(item["category"]),
                summary=str(item["summary"]),
                evidence_refs=tuple(item.get("evidence_refs", [])),
            )
            for item in raw["findings"]
        )
        verdict = raw["verdict"]
        if any(item.status == "fail" for item in acceptance_results) or expected_scope.outside_scope_paths:
            verdict = "fail"
        if any(item.status == "indeterminate" for item in acceptance_results) and verdict == "pass":
            verdict = "indeterminate"
        estimate = proposal.budget_estimate.model_dump(mode="json")
        actual = dict(budget_actual)
        variance = {
            key: actual.get(key, 0) - value
            for key, value in estimate.items()
            if isinstance(value, (int, float)) and isinstance(actual.get(key, 0), (int, float))
        }
        return AuditReport(
            audit_id=audit_id,
            proposal_id=proposal.id,
            generated_at=self.clock.now_iso(),
            auditor=Auditor(
                node_id="S3STAR_AUDITOR",
                role="S3STAR_AUDITOR",
                backend=str(getattr(self.runtime, "backend_name")),
                model=str(getattr(self.runtime, "model")),
                reasoning_effort="ultra",
                session_ref=str(getattr(self.runtime, "session_ref")),
                independent=True,
            ),
            candidate=AuditCandidate(
                base_sha=candidate.base_sha,
                commit_sha=candidate.commit_sha,
                tree_sha=candidate.tree_sha,
                diff_ref=diff_ref,
                diff_sha256=sha256_file(diff_path),
            ),
            inputs=AuditInputs(
                proposal_manifest_ref=proposal_manifest_ref,
                proposal_manifest_sha256=proposal.sha256(),
                gate_report_ref=gate_report_ref,
                gate_report_sha256=sha256_file(gate_report_path),
                raw_logs=tuple(
                    result.log_ref for result in gate_report.gates.values()
                ),
            ),
            acceptance_results=acceptance_results,
            scope_check=ScopeCheck(
                status="pass" if not expected_scope.outside_scope_paths else "fail",
                changed_paths=tuple(changed_paths),
                outside_scope_paths=expected_scope.outside_scope_paths,
            ),
            budget=AuditBudget(estimate=estimate, actual=actual, variance=variance),
            findings=findings,
            verdict=verdict,
            summary=raw["summary"],
        )


AuditRunner = S3StarAuditRunner

__all__ = ["AuditError", "AuditRunner", "S3StarAuditRunner"]
