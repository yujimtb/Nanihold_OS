"""Web/API から独立した headless self-development controller。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

from vsm.agents import AgentRequest, AgentRuntimeProtocol
from vsm.clock import Clock, SystemClock, format_iso_ms
from vsm.selfdev.artifacts import SelfDevArtifactLayout, sha256_file
from vsm.selfdev.audit import S3StarAuditRunner
from vsm.selfdev.consortium_adapter import (
    ConsortiumAdapterError,
    HumanTimeout,
    HumanTimeoutPolicy,
    SelfDevConsortiumAdapter,
)
from vsm.selfdev.effects import EffectInDoubt, EffectJournal
from vsm.selfdev.git import CandidateCommit, git_output
from vsm.selfdev.models import (
    AuditReport,
    ConsortiumDecision,
    GateReport,
    PRDescription,
    ProposalManifest,
    RunRuntime,
    proposal_to_run_manifest,
)
from vsm.selfdev.recovery import ControllerLease, ControllerRecovery
from vsm.selfdev.reasons import exception_reason, nonempty_reason
from vsm.selfdev.state_machine import PauseKind, ProposalPhase, ProposalStateMachine
from vsm.selfdev.store import SelfDevEventStore
from vsm.selfdev.workspace import ProposalWorkspace, WorkspaceStatus
from vsm.runtime.manifest import RunManifest
from vsm.roles import SystemRole


class ControllerError(RuntimeError):
    """controller の継続不能な protocol/状態エラー。"""


class ControllerPaused(ControllerError):
    """Proposal が durable pause 中であるため進められない。"""


class QuotaWait(ControllerError):
    """Run が quota wait に移行した。reset_at は必須である。"""

    def __init__(self, *, pool_id: str, reset_at: datetime, reason: str = "quota exhausted") -> None:
        if reset_at.tzinfo is None:
            raise ValueError("quota reset_at は timezone-aware でなければなりません")
        self.pool_id = pool_id
        self.reset_at = reset_at
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ImplementationResult:
    tokens: int = 0
    active_wall_clock_seconds: int = 0
    quota_wait_seconds: int = 0
    session_ref: str | None = None


class ImplementationRunTimeout(TimeoutError):
    """Proposal の implementation Run 全体の wall-clock timer が発火した。"""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"implementation run timer ({timeout_seconds:g} seconds) expired"
        )


class BackendInvocationTimeout(TimeoutError):
    """Agent backend 単発呼び出し自身の timer が発火した。"""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"backend invocation timer ({timeout_seconds:g} seconds) expired"
        )


class ImplementationRunner(Protocol):
    async def run(self, *, manifest: RunManifest, worktree: Path, resume: bool) -> Any: ...


class GateRunner(Protocol):
    async def run(self, *, manifest: RunManifest, worktree: Path, output_dir: Path) -> Any: ...


class AuditRunner(Protocol):
    async def run(self, **kwargs: Any) -> AuditReport: ...


class _RuntimeImplementationRunner:
    def __init__(self, runtime: AgentRuntimeProtocol) -> None:
        self.runtime = runtime

    async def run(self, *, manifest: RunManifest, worktree: Path, resume: bool) -> ImplementationResult:
        try:
            result = await self.runtime.invoke(
                AgentRequest(
                    prompt=(
                        "Implement the Proposal in this isolated self-development worktree. "
                        "Do not run git stage/commit/push/merge."
                    ),
                    workdir=worktree,
                    # selfdev の implementation Run も毎回新規セッションで開始する。
                    # 実 runtime は lifetime session 属性を持たず、resume 用の参照を保持しない。
                    session_ref=None,
                )
            )
        except asyncio.TimeoutError as exc:
            raise BackendInvocationTimeout(self.runtime.timeout_seconds) from exc
        if result.quota_exhausted:
            if result.quota_reset_at is None or result.quota_kind == "unknown":
                raise ControllerError("quota exhausted だが reset_at/quota_kind がありません")
            raise QuotaWait(
                pool_id=result.quota_kind,
                reset_at=result.quota_reset_at,
                reason="implementation runtime quota exhausted",
            )
        return ImplementationResult(
            tokens=result.tokens_in + result.tokens_out,
            active_wall_clock_seconds=max(0, result.latency_ms // 1000),
            session_ref=result.session_ref,
        )


class SelfDevController:
    """自己開発 Proposal を headless に一周させる状態駆動 controller。"""

    def __init__(
        self,
        *,
        repository: Path,
        store: SelfDevEventStore,
        writer_runtime: RunRuntime,
        implementation_runner: ImplementationRunner | Callable[..., Any],
        gate_runner: GateRunner | Callable[..., Any],
        audit_runner: AuditRunner,
        consortium: SelfDevConsortiumAdapter | None = None,
        consortium_runtimes: Mapping[SystemRole | str, AgentRuntimeProtocol] | None = None,
        timeout_policy: HumanTimeoutPolicy | None = None,
        worktree_root: Path,
        base_ref: str = "main",
        clock: Clock | None = None,
        implementation_timeout_margin_seconds: float = 300.0,
    ) -> None:
        self.repository = repository.resolve(strict=False)
        self.store = store
        self.layout: SelfDevArtifactLayout = store.layout
        self.writer_runtime = writer_runtime
        self.implementation_runner = implementation_runner
        self.gate_runner = gate_runner
        self.audit_runner = audit_runner
        self.worktree_root = worktree_root.resolve(strict=False)
        self.base_ref = base_ref
        self.clock = clock or SystemClock()
        if (
            not isinstance(implementation_timeout_margin_seconds, (int, float))
            or isinstance(implementation_timeout_margin_seconds, bool)
            or implementation_timeout_margin_seconds < 0
        ):
            raise ValueError("implementation_timeout_margin_seconds は0以上でなければなりません")
        self.implementation_timeout_margin_seconds = float(implementation_timeout_margin_seconds)
        self.lease = ControllerLease(self.layout.lock_path)
        self.recovery = ControllerRecovery(store)
        self.effects = EffectJournal(store)
        self._started = False
        self._fatal: Exception | None = None
        self._recovery_snapshot = None
        self._last_results: dict[str, Any] = {}
        self._protected_approvals: dict[str, dict[str, str]] = {}
        self._budget_actual: dict[str, Any] = {}
        if consortium is None:
            if consortium_runtimes is None or timeout_policy is None:
                raise ValueError("consortium または consortium_runtimes と timeout_policy が必要です")
            consortium = SelfDevConsortiumAdapter(
                store=store,
                runtimes=consortium_runtimes,
                clock=self.clock,
                timeout_policy=timeout_policy,
            )
        self.consortium = consortium

    async def start(self) -> None:
        if self._started:
            return
        try:
            await self.store.start()
            self.lease.acquire()
            snapshot = self.recovery.reconcile()
            await self._record_integrity_failures(snapshot)
            snapshot = self.recovery.reconcile()
            self._recovery_snapshot = snapshot
            self._started = True
            integrity_ids = {item.proposal_id for item in snapshot.integrity_failures}
            if (
                snapshot.in_doubt_effects
                and snapshot.active_proposal_id is not None
                and snapshot.active_proposal_id not in integrity_ids
            ):
                await self._pause_for_recovery(snapshot.active_proposal_id, snapshot.in_doubt_effects)
        except Exception:
            self.lease.release()
            await self.store.stop()
            raise

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        await self.store.stop()
        self.lease.release()

    def _require_started(self) -> None:
        if not self._started:
            raise ControllerError("SelfDevController.start() 前に操作できません")
        if self._fatal is not None:
            raise ControllerError(f"controller は degraded 停止中です: {self._fatal}")

    @property
    def integrity_failures(self) -> tuple[Any, ...]:
        snapshot = self._recovery_snapshot or self.recovery.last_snapshot
        return snapshot.integrity_failures if snapshot is not None else ()

    async def _record_integrity_failures(self, snapshot: Any) -> None:
        for failure in snapshot.integrity_failures:
            if failure.recorded:
                continue
            await self.store.append(
                "proposal_integrity_failed",
                {
                    "proposal_id": failure.proposal_id,
                    "phase": failure.phase.value,
                    "disposition": failure.disposition,
                    "failure_kind": failure.failure_kind,
                    "artifact_ref": failure.artifact_ref,
                    "reason": failure.reason,
                },
                proposal_id=failure.proposal_id,
                actor_type="controller",
            )

    def _projection(self, proposal_id: str):
        projection = self.store.projection(proposal_id)
        if projection is None:
            raise KeyError(f"Proposal がありません: {proposal_id}")
        return projection

    def _manifest(self, proposal_id: str) -> ProposalManifest:
        path = self.layout.proposal_manifest_path(proposal_id)
        if not path.exists():
            raise ControllerError(f"ProposalManifest がありません: {proposal_id}")
        manifest = ProposalManifest.model_validate_json(path.read_text(encoding="utf-8"))
        self._restore_protected_approval(manifest)
        return manifest

    def _restore_protected_approval(self, proposal: ProposalManifest) -> None:
        if proposal.risk_class != "protected" or proposal.id in self._protected_approvals:
            return
        approvals = [
            event for event in self._events(proposal.id)
            if event.event_type == "human_review_responded" and event.payload.get("decision") == "approve"
        ]
        if len(approvals) > 1:
            raise ControllerError("protected Human approval が二重です")
        if approvals:
            self._protected_approvals[proposal.id] = {
                "event_id": approvals[0].event_id,
                "proposal_manifest_sha256": proposal.sha256(),
                "protected_scope_sha256": self._scope_sha(proposal),
            }

    def _proposal_dir(self, proposal_id: str) -> Path:
        return self.layout.proposal_dir(proposal_id)

    def _events(self, proposal_id: str) -> list[Any]:
        return [event for event in self.store.read_events() if event.payload.get("proposal_id") == proposal_id]

    async def submit_proposal(self, proposal: ProposalManifest) -> str:
        self._require_started()
        active = [
            item for item in self.store.replay().values()
            if not item.aggregate.is_terminal
        ]
        if active:
            raise ControllerError("active Proposal slot は既に使用中です")
        path, _ = self.layout.write_proposal_manifest(proposal)
        await self.store.append(
            "proposal_state_changed",
            {
                "proposal_id": proposal.id,
                "from_state": None,
                "to_state": "PROPOSED",
                "reason_code": "proposal_created",
                "reason": "ProposalManifestを受理した",
                "related_run_id": None,
                "decision_event_id": None,
                "artifact_refs": (),
            },
            proposal_id=proposal.id,
            actor_type="controller",
        )
        await self._record_artifact(proposal.id, "proposal_manifest", path, relative="proposal.json")
        return proposal.id

    async def _record_artifact(self, proposal_id: str, kind: str, path: Path, *, relative: str | None = None) -> None:
        root = self._proposal_dir(proposal_id)
        ref = relative or path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
        digest = sha256_file(path)
        existing = [
            event for event in self._events(proposal_id)
            if event.event_type == "artifact_created" and event.payload.get("ref") == ref
        ]
        if existing:
            if len(existing) != 1 or existing[0].payload.get("sha256") != digest:
                raise ControllerError(f"artifact ref の hash が二重または不一致です: {ref}")
            return
        await self.store.append(
            "artifact_created",
            {
                "proposal_id": proposal_id,
                "artifact_kind": kind,
                "ref": ref,
                "sha256": digest,
            },
            proposal_id=proposal_id,
            actor_type="controller",
            schema_version=2,
        )

    async def _transition(
        self,
        proposal_id: str,
        target: ProposalPhase,
        *,
        reason_code: str,
        reason: str,
        related_run_id: str | None = None,
        decision_event_id: str | None = None,
        artifact_refs: tuple[str, ...] = (),
    ) -> Any:
        projection = self._projection(proposal_id)
        machine = ProposalStateMachine(projection.aggregate)
        machine.transition(target, allow_while_paused=target is ProposalPhase.ABORTED, active_run_id=related_run_id)
        return await self.store.append(
            "proposal_state_changed",
            {
                "proposal_id": proposal_id,
                "from_state": projection.aggregate.phase.value,
                "to_state": target.value,
                "reason_code": reason_code,
                "reason": nonempty_reason(reason, context="proposal state transition"),
                "related_run_id": related_run_id,
                "decision_event_id": decision_event_id,
                "artifact_refs": artifact_refs,
            },
            proposal_id=proposal_id,
            actor_type="controller",
        )

    async def _append_generic(self, event_type: str, proposal_id: str, payload: dict[str, Any], *, actor_type: str = "controller") -> Any:
        payload = dict(payload)
        payload.setdefault("proposal_id", proposal_id)
        return await self.store.append(event_type, payload, proposal_id=proposal_id, actor_type=actor_type)

    async def _pause_for_recovery(
        self,
        proposal_id: str,
        effects: tuple[tuple[str, str], ...],
        *,
        reason: str = "effect in doubt; external fact verification is required",
    ) -> None:
        notification = await self._append_generic(
            "algedonic_human_notification",
            proposal_id,
            {"reason": nonempty_reason(reason, context="recovery pause"), "effects": [list(item) for item in effects]},
        )
        await self.store.append(
            "proposal_pause_changed",
            {
                "proposal_id": proposal_id,
                "action": "added",
                "pause_id": f"pause-recovery-{notification.event_id}",
                "cause": "SUSPEND",
                "actor_type": "controller",
                "actor_id": "controller",
                "pool_id": None,
                "reset_at": None,
                "source_event_id": notification.event_id,
                "reason": "副作用の外部事実を証明できないため停止",
            },
            proposal_id=proposal_id,
        )

    def implementation_timeout_seconds(self, proposal: ProposalManifest) -> float:
        """Proposal budget から implementation Run の外側 timer を導出する。"""

        timeout_seconds = (
            float(proposal.budget_estimate.active_wall_clock_seconds)
            + self.implementation_timeout_margin_seconds
        )
        if timeout_seconds <= 0:
            raise ControllerError(
                "implementation Run の timeout は active wall-clock budget または余裕を必要とします"
            )
        return timeout_seconds

    def _decision(self, proposal_id: str, review_kind: str) -> tuple[Any, ConsortiumDecision] | None:
        decisions = [
            event for event in self._events(proposal_id)
            if event.event_type == "consortium_decided" and event.payload.get("review_kind") == review_kind
        ]
        if len(decisions) > 1:
            raise ControllerError(f"{review_kind} Consortium decision が二重です")
        if not decisions:
            return None
        event = decisions[0]
        return event, ConsortiumDecision.model_validate(event.payload)

    async def _record_consortium_decision(self, decision: ConsortiumDecision) -> Any:
        return await self.store.append(
            "consortium_decided",
            decision.model_dump(mode="json"),
            proposal_id=decision.proposal_id,
            actor_type="node",
            actor_id=SystemRole.S5_POLICY.value,
            schema_version=2,
        )

    def _initial_dossier(self, proposal: ProposalManifest) -> dict[str, Any]:
        local_main_sha = git_output(self.repository, "rev-parse", self.base_ref).strip()
        if not local_main_sha:
            raise ControllerError("initial dossier の main SHA が空です")
        return {
            "proposal_manifest": proposal.canonical_dict(),
            "manifest_hash": proposal.sha256(),
            "source_context_refs": [],
            "dependency_states": {item: "UNKNOWN" for item in proposal.dependencies},
            "scope_conflicts": [],
            "protected_path": proposal.risk_class == "protected",
            "human_approval_status": "pending" if proposal.risk_class == "protected" else "not_required",
            "local_main_sha": local_main_sha,
            "quota_admission": {},
            "context_view_refs": [],
            "decision_contract": {"decision": ["APPROVE", "REJECT"]},
        }

    async def _run_initial_review(self, proposal: ProposalManifest) -> None:
        existing = self._decision(proposal.id, "initial")
        if existing is None:
            consortium_id = f"consortium-{proposal.id}-initial"
            if not any(
                event.event_type == "consortium_convened" and event.payload.get("consortium_id") == consortium_id
                for event in self._events(proposal.id)
            ):
                await self._append_generic(
                    "consortium_convened",
                    proposal.id,
                    {
                        "consortium_id": consortium_id,
                        "subject": proposal.title,
                        "participants": [role.value for role in (SystemRole.S3_ALLOCATOR, SystemRole.S4_SCANNER, SystemRole.S5_POLICY,)],
                        "convener": SystemRole.S5_POLICY.value,
                        "rounds": 2,
                        "trigger": "selfdev_initial",
                    },
                )
            dossier = self._initial_dossier(proposal)
            dossier_path = self._proposal_dir(proposal.id) / "artifacts" / "initial-dossier.json"
            dossier_sha = self.layout.write_json(dossier_path, dossier, immutable=True)
            await self._record_artifact(proposal.id, "consortium_initial_dossier", dossier_path, relative="artifacts/initial-dossier.json")
            try:
                decision = await self.consortium.convene(
                    proposal=proposal,
                    consortium_id=consortium_id,
                    review_kind="initial",
                    dossier=dossier,
                    dossier_ref="artifacts/initial-dossier.json",
                    human=True,
                    dossier_sha256=dossier_sha,
                )
            except HumanTimeout:
                await self._abort(proposal.id, "human timeout", reason_code="human_timeout")
                return
            except ConsortiumAdapterError as exc:
                await self._abort(
                    proposal.id,
                    exc,
                    reason_code="aborted",
                    reason_context="initial consortium review",
                )
                return
            if decision.dossier_sha256 != dossier_sha:
                raise ControllerError("Consortium dossier hash が artifact と一致しません")
            event = await self._record_consortium_decision(decision)
            existing = (event, decision)
        event, decision = existing
        if decision.decision == "REJECT":
            await self._transition(
                proposal.id,
                ProposalPhase.REJECTED,
                reason_code="consortium_rejected",
                reason=decision.reason,
                decision_event_id=event.event_id,
            )
            return
        if proposal.risk_class == "protected":
            if self._projection(proposal.id).aggregate.phase is ProposalPhase.CONSORTIUM_REVIEW:
                await self._transition(
                    proposal.id,
                    ProposalPhase.NEEDS_HUMAN,
                    reason_code="human_decision_required",
                    reason="protected path の明示 Human approval が必要です",
                    decision_event_id=event.event_id,
                )
            return
        await self._transition(
            proposal.id,
            ProposalPhase.APPROVED,
            reason_code="consortium_approved",
            reason=decision.reason,
            decision_event_id=event.event_id,
        )

    async def respond_human(self, proposal_id: str, *, decision: str, response: str) -> Any:
        self._require_started()
        proposal = self._manifest(proposal_id)
        initial = self._decision(proposal_id, "initial")
        if initial is None:
            raise ControllerError("initial Consortium decision がありません")
        if decision == "approve" and proposal.risk_class != "protected":
            raise ValueError("明示 Human approval は protected Proposal にだけ必要です")
        return await self.consortium.respond_human(
            proposal_id=proposal_id,
            consortium_id=initial[1].consortium_id,
            decision=decision,
            response=response,
        )

    async def _handle_needs_human(self, proposal: ProposalManifest) -> None:
        initial = self._decision(proposal.id, "initial")
        if initial is None:
            raise ControllerError("NEEDS_HUMAN に initial decision がありません")
        responses = [
            event for event in self._events(proposal.id)
            if event.event_type == "human_review_responded"
            and event.payload.get("review_id") == f"review-{initial[1].consortium_id}"
        ]
        if not responses:
            return
        response = responses[-1]
        if response.payload["decision"] == "reject":
            await self._transition(
                proposal.id,
                ProposalPhase.REJECTED,
                reason_code="human_rejected",
                reason=response.payload["response"],
                decision_event_id=response.event_id,
            )
            return
        if response.payload["decision"] != "approve":
            return
        self._protected_approvals[proposal.id] = {
            "event_id": response.event_id,
            "proposal_manifest_sha256": proposal.sha256(),
            "protected_scope_sha256": self._scope_sha(proposal),
        }
        await self._transition(
            proposal.id,
            ProposalPhase.APPROVED,
            reason_code="human_approved",
            reason="protected path の明示 Human approval を受理しました",
            decision_event_id=response.event_id,
        )

    @staticmethod
    def _scope_sha(proposal: ProposalManifest) -> str:
        from vsm.selfdev.verification import scope_sha256

        return scope_sha256([rule.model_dump(mode="json") for rule in proposal.scope])

    async def _link_run(self, proposal: ProposalManifest, *, attempt: int, parent_run_id: str | None) -> tuple[RunManifest, Any]:
        if attempt == 2 and not parent_run_id:
            raise ControllerError("repair Run の parent_run_id がありません")
        if git_output(self.repository, "status", "--porcelain").strip():
            raise ControllerError("main checkout が汚染されているため selfdev workspace を開始できません")
        base_sha = git_output(self.repository, "rev-parse", self.base_ref).strip()
        if not base_sha:
            raise ControllerError("main base SHA が空です")
        run_id = "run-" + hashlib.sha256(f"{proposal.id}:{attempt}".encode("ascii")).hexdigest()[:32]
        manifest = proposal_to_run_manifest(
            proposal,
            repository=self.repository,
            base_sha=base_sha,
            worktree_path=self.worktree_root / proposal.id,
            initial_decision_event_id=self._decision(proposal.id, "initial")[0].event_id,  # type: ignore[index]
            writer_runtime=self.writer_runtime,
            run_id=run_id,
            attempt=attempt,  # type: ignore[arg-type]
            parent_run_id=parent_run_id,
            protected_approval_event_id=self._protected_approvals.get(proposal.id, {}).get("event_id"),
            created_at=self.clock.now_iso(),
        )
        run_dir = self._proposal_dir(proposal.id) / "runs" / run_id
        manifest_path = manifest.persist(run_dir)
        manifest_ref = f"runs/{run_id}/manifest.json"
        kind = "repair" if attempt == 2 else "implementation"
        event = await self.store.append(
            "proposal_run_linked",
            {
                "proposal_id": proposal.id,
                "run_id": run_id,
                "run_kind": kind,
                "attempt": attempt,
                "parent_run_id": parent_run_id,
                "manifest_ref": manifest_ref,
                "manifest_sha256": sha256_file(manifest_path),
            },
            proposal_id=proposal.id,
            actor_type="controller",
        )
        self._last_results[run_id] = manifest
        return manifest, event

    def _workspace(self, manifest: RunManifest, proposal_id: str) -> ProposalWorkspace:
        return ProposalWorkspace(manifest=manifest, run_dir=self._proposal_dir(proposal_id))

    async def _handle_approved(self, proposal: ProposalManifest) -> None:
        projection = self._projection(proposal.id)
        try:
            if projection.aggregate.active_run_id is None:
                manifest, _ = await self._link_run(proposal, attempt=1, parent_run_id=None)
            else:
                manifest = self._load_run_manifest(proposal.id, projection.aggregate.active_run_id)
            workspace = self._workspace(manifest, proposal.id)
            effect_id = "workspace:create"
            executed, value = await self.effects.run(
                proposal_id=proposal.id,
                effect_id=effect_id,
                effect_kind="workspace",
                input_value=manifest.to_dict(),
                operation=workspace.create,
                artifact_refs=("workspace.json",),
            )
            del value
            if not executed and not workspace.descriptor_path.exists():
                raise ControllerError("workspace effect completed だが descriptor がありません")
            await self._record_artifact(proposal.id, "workspace_descriptor", workspace.descriptor_path, relative="workspace.json")
            await self._transition(
                proposal.id,
                ProposalPhase.WORKSPACE_READY,
                reason_code="workspace_ready",
                reason="Proposal workspace を作成しました",
                related_run_id=manifest.run_id,
            )
        except Exception as exc:
            await self._abort(proposal.id, exc, reason_code="aborted", reason_context="workspace setup")

    async def _invoke_implementation(self, manifest: RunManifest, worktree: Path, *, resume: bool) -> Any:
        runner = self.implementation_runner
        if hasattr(runner, "run"):
            value = runner.run(manifest=manifest, worktree=worktree, resume=resume)  # type: ignore[union-attr]
        else:
            value = runner(manifest=manifest, worktree=worktree, resume=resume)  # type: ignore[operator]
        return await value if inspect.isawaitable(value) else value

    async def _invoke_implementation_with_timeout(
        self,
        manifest: RunManifest,
        worktree: Path,
        *,
        resume: bool,
        timeout_seconds: float,
    ) -> Any:
        """単発 backend timer と別の implementation Run 全体 timer。"""

        task = asyncio.create_task(
            self._invoke_implementation(manifest, worktree, resume=resume),
            name=f"selfdev-implementation-{manifest.run_id}",
        )
        try:
            done, pending = await asyncio.wait({task}, timeout=timeout_seconds)
            if pending:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise ImplementationRunTimeout(timeout_seconds)
            return task.result()
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    async def _handle_workspace_ready(self, proposal: ProposalManifest) -> None:
        projection = self._projection(proposal.id)
        run_id = projection.aggregate.active_run_id
        if not run_id:
            raise ControllerError("WORKSPACE_READY に active implementation Run がありません")
        manifest = self._load_run_manifest(proposal.id, run_id)
        workspace = self._workspace(manifest, proposal.id)
        try:
            implementation_timeout = self.implementation_timeout_seconds(proposal)
            _, result = await self.effects.run(
                proposal_id=proposal.id,
                effect_id=f"run:{run_id}",
                effect_kind="run",
                input_value={
                    "run_id": run_id,
                    "attempt": manifest.attempt,
                    "resume": False,
                    "implementation_run_timeout_seconds": implementation_timeout,
                },
                operation=lambda: self._invoke_implementation_with_timeout(
                    manifest,
                    workspace.acquire(),
                    resume=False,
                    timeout_seconds=implementation_timeout,
                ),
            )
            if isinstance(result, EffectInDoubt):
                raise result
            if isinstance(result, ImplementationResult):
                self._budget_actual = {
                    "tokens": result.tokens,
                    "active_wall_clock_seconds": result.active_wall_clock_seconds,
                    "quota_wait_seconds": result.quota_wait_seconds,
                }
            await self._transition(
                proposal.id,
                ProposalPhase.IMPLEMENTING,
                reason_code="implementation_started",
                reason="implementation Run が完了しました",
                related_run_id=run_id,
            )
            await self._transition(
                proposal.id,
                ProposalPhase.GATES_RUNNING,
                reason_code="implementation_completed",
                reason="implementation Run の成果物を GateRunner に渡します",
                related_run_id=run_id,
            )
        except QuotaWait as exc:
            await self.quota_wait(
                proposal.id,
                pool_id=exc.pool_id,
                reset_at=exc.reset_at,
                reason=exception_reason(exc, context="implementation run quota wait"),
            )
        except Exception as exc:
            await self._abort(
                proposal.id,
                exc,
                reason_code="aborted",
                reason_context="implementation run",
            )

    async def _invoke_gate(self, manifest: RunManifest, worktree: Path, output_dir: Path) -> Any:
        runner = self.gate_runner
        if hasattr(runner, "run"):
            value = runner.run(manifest=manifest, worktree=worktree, output_dir=output_dir)  # type: ignore[union-attr]
        else:
            value = runner(manifest=manifest, worktree=worktree, output_dir=output_dir)  # type: ignore[operator]
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _gate_report(value: Any) -> GateReport:
        if isinstance(value, GateReport):
            return value
        if isinstance(value, tuple) and value and isinstance(value[0], Mapping):
            value = value[0]
        if not isinstance(value, Mapping):
            raise ControllerError("GateRunner は GateReport または mapping を返さなければなりません")
        return GateReport.model_validate(value)

    def _load_gate_report(self, proposal_id: str, attempt: int) -> GateReport:
        path = self.layout.gates_dir(proposal_id) / f"attempt-{attempt}" / "gate_report.json"
        if not path.exists():
            raise ControllerError(f"GateReport artifact がありません: {path}")
        return GateReport.model_validate_json(path.read_text(encoding="utf-8"))

    async def _handle_gates_running(self, proposal: ProposalManifest) -> None:
        projection = self._projection(proposal.id)
        run_id = projection.aggregate.active_run_id
        if not run_id:
            raise ControllerError("GATES_RUNNING に active Run がありません")
        manifest = self._load_run_manifest(proposal.id, run_id)
        attempt = manifest.attempt
        output_dir = self.layout.gates_dir(proposal.id) / f"attempt-{attempt}"
        workspace = self._workspace(manifest, proposal.id)
        effect_id = f"gate:{attempt}"
        try:
            completed = self.effects.completed(proposal.id, effect_id)
            if completed is not None:
                report = self._load_gate_report(proposal.id, attempt)
            else:
                async def gate_operation() -> GateReport:
                    raw = await self._invoke_gate(manifest, workspace.acquire(), output_dir)
                    generated = self._gate_report(raw)
                    report_path = output_dir / "gate_report.json"
                    if report_path.exists():
                        existing_report = GateReport.model_validate_json(report_path.read_text(encoding="utf-8"))
                        if existing_report != generated:
                            raise ControllerError("GateRunner の既存 report と returned report が不一致です")
                    else:
                        self.layout.write_json(
                            report_path,
                            generated.model_dump(mode="json"),
                            immutable=True,
                        )
                    return generated

                value = await self.effects.run(
                    proposal_id=proposal.id,
                    effect_id=effect_id,
                    effect_kind="gate",
                    input_value={"run_id": run_id, "attempt": attempt, "base_sha": manifest.base_sha},
                    operation=gate_operation,
                    artifact_refs=(f"gates/attempt-{attempt}/gate_report.json",),
                )
                result = value[1]
                if not isinstance(result, GateReport):
                    raise ControllerError("Gate effect は GateReport を返さなければなりません")
                report = result
            await self._record_artifact(proposal.id, "gate_report", output_dir / "gate_report.json", relative=f"gates/attempt-{attempt}/gate_report.json")
            gate_events = [
                event for event in self._events(proposal.id)
                if event.event_type == "gate_report_generated"
                and event.payload.get("gate_attempt") == attempt
            ]
            if len(gate_events) > 1:
                raise ControllerError("同じ gate attempt の report event が二重です")
            gate_event = gate_events[0] if gate_events else await self.store.append(
                "gate_report_generated",
                {
                    "proposal_id": proposal.id,
                    "implementation_run_id": report.implementation_run_id,
                    "gate_attempt": report.gate_attempt,
                    "report_ref": f"gates/attempt-{attempt}/gate_report.json",
                    "status": report.status,
                    "gate_statuses": {name: detail.status for name, detail in report.gates.items()},
                    "scope_sha256": report.scope_sha256,
                    "candidate_diff_sha256": report.candidate_diff_sha256,
                },
                proposal_id=proposal.id,
                actor_type="trusted_gate_runner",
                schema_version=2,
            )
            if report.status == "pass":
                await self._transition(proposal.id, ProposalPhase.GATES_PASSED, reason_code="gates_passed", reason="G1〜G4 が成功しました", related_run_id=run_id, artifact_refs=(f"gates/attempt-{attempt}/gate_report.json",))
            elif report.status == "fail" and attempt == 1:
                await self._transition(proposal.id, ProposalPhase.GATES_FAILED, reason_code="gates_failed", reason="Gate が失敗しました。repair 枠を保持します", related_run_id=run_id, artifact_refs=(f"gates/attempt-{attempt}/gate_report.json",))
            elif report.status == "fail":
                await self._abort(proposal.id, "repair 後も Gate が失敗しました", reason_code="repair_exhausted")
            else:
                await self._abort(proposal.id, "Gate execution error", reason_code="aborted")
        except QuotaWait as exc:
            await self.quota_wait(
                proposal.id,
                pool_id=exc.pool_id,
                reset_at=exc.reset_at,
                reason=exception_reason(exc, context="gate run quota wait"),
            )
        except Exception as exc:
            await self._abort(proposal.id, exc, reason_code="aborted", reason_context="gate run")

    async def _handle_gates_failed(self, proposal: ProposalManifest) -> None:
        projection = self._projection(proposal.id)
        try:
            repair_links = [link for link in projection.run_links if link.get("run_kind") == "repair"]
            if len(repair_links) > 1:
                raise ControllerError("repair Run が二重に link されています")
            if repair_links:
                run_id = str(repair_links[0]["run_id"])
                manifest = self._load_run_manifest(proposal.id, run_id)
                parent_run_id = manifest.parent_run_id
            else:
                if projection.aggregate.repair_used:
                    await self._abort(proposal.id, "repair 枠を使い切りました", reason_code="repair_exhausted")
                    return
                parent_run_id = projection.aggregate.active_run_id
                if not parent_run_id:
                    raise ControllerError("GATES_FAILED に parent Run がありません")
                manifest, _ = await self._link_run(proposal, attempt=2, parent_run_id=parent_run_id)
            workspace = self._workspace(manifest, proposal.id)
            if self.effects.completed(proposal.id, f"run:{manifest.run_id}") is None:
                implementation_timeout = self.implementation_timeout_seconds(proposal)
                await self.effects.run(
                    proposal_id=proposal.id,
                    effect_id=f"run:{manifest.run_id}",
                    effect_kind="run",
                    input_value={
                        "run_id": manifest.run_id,
                        "parent_run_id": parent_run_id,
                        "attempt": 2,
                        "implementation_run_timeout_seconds": implementation_timeout,
                    },
                    operation=lambda: self._invoke_implementation_with_timeout(
                        manifest,
                        workspace.acquire(),
                        resume=False,
                        timeout_seconds=implementation_timeout,
                    ),
                )
            await self._transition(proposal.id, ProposalPhase.GATES_RUNNING, reason_code="repair_completed", reason="1回限りの repair Run が完了しました", related_run_id=manifest.run_id)
        except QuotaWait as exc:
            await self.quota_wait(
                proposal.id,
                pool_id=exc.pool_id,
                reset_at=exc.reset_at,
                reason=exception_reason(exc, context="repair run quota wait"),
            )
        except Exception as exc:
            await self._abort(proposal.id, exc, reason_code="aborted", reason_context="repair run")

    def _load_run_manifest(self, proposal_id: str, run_id: str) -> RunManifest:
        return RunManifest.load(self._proposal_dir(proposal_id) / "runs" / run_id)

    def _load_candidate_commit(self, proposal_id: str) -> CandidateCommit:
        path = self._proposal_dir(proposal_id) / "artifacts" / "candidate-commit.json"
        if not path.exists():
            raise ControllerError("candidate commit artifact がありません")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return CandidateCommit(**payload)

    async def _handle_gates_passed(self, proposal: ProposalManifest) -> None:
        projection = self._projection(proposal.id)
        run_id = projection.aggregate.active_run_id
        if not run_id:
            raise ControllerError("GATES_PASSED に active Run がありません")
        manifest = self._load_run_manifest(proposal.id, run_id)
        report = self._load_gate_report(proposal.id, manifest.attempt)
        workspace = self._workspace(manifest, proposal.id)
        try:
            completed = self.effects.completed(proposal.id, "commit:candidate")
            if completed is not None:
                candidate = self._load_candidate_commit(proposal.id)
            else:
                def commit() -> CandidateCommit:
                    value = workspace.commit_candidate(gate_report=report)
                    path = self._proposal_dir(proposal.id) / "artifacts" / "candidate-commit.json"
                    self.layout.write_json(path, value.to_dict(), immutable=True)
                    return value

                result = await self.effects.run(
                    proposal_id=proposal.id,
                    effect_id="commit:candidate",
                    effect_kind="commit",
                    input_value={"run_id": run_id, "diff_sha256": report.candidate_diff_sha256},
                    operation=commit,
                    artifact_refs=("artifacts/candidate-commit.json",),
                )
                candidate = result[1] if isinstance(result[1], CandidateCommit) else self._load_candidate_commit(proposal.id)
            await self._record_artifact(proposal.id, "candidate_commit", self._proposal_dir(proposal.id) / "artifacts" / "candidate-commit.json", relative="artifacts/candidate-commit.json")
            patch_path = workspace.snapshot()
            await self._record_artifact(proposal.id, "candidate_patch", patch_path, relative="artifacts/candidate.patch")
            self._last_results[proposal.id] = candidate
            await self._transition(proposal.id, ProposalPhase.AUDIT, reason_code="audit_started", reason="candidate commit を S3★独立監査へ提出します", related_run_id=run_id)
        except Exception as exc:
            await self._abort(proposal.id, exc, reason_code="aborted", reason_context="candidate commit")

    def _load_audit_report(self, proposal_id: str) -> AuditReport:
        path = self.layout.audit_dir(proposal_id) / "audit_report.json"
        if not path.exists():
            raise ControllerError("audit report artifact がありません")
        return AuditReport.model_validate_json(path.read_text(encoding="utf-8"))

    async def _handle_audit(self, proposal: ProposalManifest) -> None:
        projection = self._projection(proposal.id)
        run_id = projection.aggregate.active_run_id
        if not run_id:
            raise ControllerError("AUDIT に active Run がありません")
        manifest = self._load_run_manifest(proposal.id, run_id)
        report = self._load_gate_report(proposal.id, manifest.attempt)
        candidate = self._load_candidate_commit(proposal.id)
        workspace = self._workspace(manifest, proposal.id)
        patch_path = self._proposal_dir(proposal.id) / "artifacts" / "candidate.patch"
        gate_path = self.layout.gates_dir(proposal.id) / f"attempt-{manifest.attempt}" / "gate_report.json"
        manifest_path = self.layout.proposal_manifest_path(proposal.id)
        try:
            if self.effects.completed(proposal.id, "audit:report") is None:
                if not patch_path.exists():
                    patch_path = workspace.snapshot()
                async def audit_operation() -> AuditReport:
                    audit = await self.audit_runner.run(
                        proposal=proposal,
                        candidate=candidate,
                        gate_report=report,
                        root=self._proposal_dir(proposal.id),
                        proposal_manifest_ref="proposal.json",
                        manifest_path=manifest_path,
                        gate_report_ref=f"gates/attempt-{manifest.attempt}/gate_report.json",
                        gate_report_path=gate_path,
                        diff_ref="artifacts/candidate.patch",
                        diff_path=patch_path,
                        changed_paths=tuple(report.changed_paths),
                        protected_approval=self._protected_approvals.get(proposal.id),
                        budget_actual=self._budget_actual,
                        audit_id=f"audit-{proposal.id}",
                    )
                    if not isinstance(audit, AuditReport):
                        raise ControllerError("AuditRunner は AuditReport を返さなければなりません")
                    self.layout.write_json(
                        self.layout.audit_dir(proposal.id) / "audit_report.json",
                        audit.model_dump(mode="json"),
                        immutable=True,
                    )
                    return audit
                value = await self.effects.run(
                    proposal_id=proposal.id,
                    effect_id="audit:report",
                    effect_kind="audit",
                    input_value={"candidate": candidate.to_dict(), "gate_report": report.model_dump(mode="json")},
                    operation=audit_operation,
                    artifact_refs=("audit/audit_report.json",),
                )
                audit = value[1]
                if not isinstance(audit, AuditReport):
                    raise ControllerError("AuditRunner は AuditReport を返さなければなりません")
                audit_path = self.layout.audit_dir(proposal.id) / "audit_report.json"
            audit = self._load_audit_report(proposal.id)
            await self._record_artifact(proposal.id, "audit_report", self.layout.audit_dir(proposal.id) / "audit_report.json", relative="audit/audit_report.json")
            sent = [event for event in self._events(proposal.id) if event.event_type == "audit_report_sent"]
            if len(sent) > 1:
                raise ControllerError("audit_report_sent が二重です")
            if not sent:
                await self.store.append(
                    "audit_report_sent",
                    {
                        "proposal_id": proposal.id,
                        "audit_id": audit.audit_id,
                        "report_ref": "audit/audit_report.json",
                        "report_sha256": sha256_file(self.layout.audit_dir(proposal.id) / "audit_report.json"),
                    },
                    proposal_id=proposal.id,
                    actor_type="node",
                    actor_id="S3STAR_AUDITOR",
                    schema_version=2,
                )
            await self._transition(proposal.id, ProposalPhase.FINAL_CONSORTIUM, reason_code="audit_completed", reason=f"S3★監査 verdict={audit.verdict}", related_run_id=run_id)
        except Exception as exc:
            await self._abort(proposal.id, exc, reason_code="audit_failed", reason_context="audit run")

    def _final_dossier(self, proposal: ProposalManifest, initial: ConsortiumDecision, gate: GateReport, audit: AuditReport, candidate: CandidateCommit) -> dict[str, Any]:
        return {
            "proposal_manifest": proposal.canonical_dict(),
            "initial_decision": initial.model_dump(mode="json"),
            "protected_approval": self._protected_approvals.get(proposal.id),
            "candidate": candidate.to_dict(),
            "diff_summary": list(gate.changed_paths),
            "gate_report": gate.model_dump(mode="json"),
            "audit_report": audit.model_dump(mode="json"),
            "budget_actual": self._budget_actual,
            "repair_used": self._projection(proposal.id).aggregate.repair_used,
            "decision_contract": {"decision": ["MERGE_READY", "REJECT_FINAL"]},
        }

    async def _handle_final_consortium(self, proposal: ProposalManifest) -> None:
        initial_entry = self._decision(proposal.id, "initial")
        if initial_entry is None:
            raise ControllerError("initial decision がありません")
        candidate = self._load_candidate_commit(proposal.id)
        manifest = self._load_run_manifest(proposal.id, self._projection(proposal.id).aggregate.active_run_id or "")
        gate = self._load_gate_report(proposal.id, manifest.attempt)
        audit = self._load_audit_report(proposal.id)
        entry = self._decision(proposal.id, "final")
        try:
            if entry is None:
                consortium_id = f"consortium-{proposal.id}-final"
                if not any(event.event_type == "consortium_convened" and event.payload.get("consortium_id") == consortium_id for event in self._events(proposal.id)):
                    await self._append_generic(
                        "consortium_convened",
                        proposal.id,
                        {
                            "consortium_id": consortium_id,
                            "subject": proposal.title,
                            "participants": [role.value for role in (SystemRole.S3_ALLOCATOR, SystemRole.S4_SCANNER, SystemRole.S5_POLICY)],
                            "convener": SystemRole.S5_POLICY.value,
                            "rounds": 2,
                            "trigger": "selfdev_final",
                        },
                    )
                dossier = self._final_dossier(proposal, initial_entry[1], gate, audit, candidate)
                dossier_path = self._proposal_dir(proposal.id) / "artifacts" / "final-dossier.json"
                dossier_sha = self.layout.write_json(dossier_path, dossier, immutable=True)
                await self._record_artifact(proposal.id, "consortium_final_dossier", dossier_path, relative="artifacts/final-dossier.json")
                final = await self.consortium.convene(
                    proposal=proposal,
                    consortium_id=consortium_id,
                    review_kind="final",
                    dossier=dossier,
                    dossier_ref="artifacts/final-dossier.json",
                    human=False,
                    dossier_sha256=dossier_sha,
                )
                if final.dossier_sha256 != dossier_sha:
                    raise ControllerError("final Consortium dossier hash が artifact と一致しません")
                event = await self._record_consortium_decision(final)
                entry = (event, final)
            event, final = entry
            if final.decision == "REJECT_FINAL":
                await self._cleanup_workspace(proposal.id, phase=ProposalPhase.REJECTED_FINAL)
                await self._transition(proposal.id, ProposalPhase.REJECTED_FINAL, reason_code="final_rejected", reason=final.reason, decision_event_id=event.event_id)
                return
            description = PRDescription(
                proposal=proposal,
                initial_decision=initial_entry[1],
                protected_approval=(self._protected_approvals.get(proposal.id) or {}).get("event_id"),
                candidate_commit=candidate.commit_sha,
                diff_summary=", ".join(gate.changed_paths) or "変更なし",
                gate_report=gate,
                audit_report=audit,
                budget_actual=self._budget_actual,
                final_decision=final,
                artifact_refs=(
                    "artifacts/candidate.patch",
                    f"gates/attempt-{gate.gate_attempt}/gate_report.json",
                    "audit/audit_report.json",
                    "pr-description.md",
                ),
            )
            pr_path = self.layout.pr_description_path(proposal.id)
            self.layout.write_text(pr_path, description.render(), immutable=True)
            await self._record_artifact(proposal.id, "pr_description", pr_path, relative="pr-description.md")
            await self._cleanup_workspace(proposal.id, phase=ProposalPhase.MERGE_READY)
            await self._transition(proposal.id, ProposalPhase.MERGE_READY, reason_code="final_approved", reason=final.reason, decision_event_id=event.event_id, artifact_refs=("pr-description.md",))
        except Exception as exc:
            await self._abort(proposal.id, exc, reason_code="aborted", reason_context="final consortium")

    async def _cleanup_workspace(self, proposal_id: str, *, phase: ProposalPhase) -> None:
        projection = self._projection(proposal_id)
        run_id = projection.aggregate.active_run_id
        if not run_id:
            return
        manifest = self._load_run_manifest(proposal_id, run_id)
        workspace = self._workspace(manifest, proposal_id)
        if not workspace.descriptor_path.exists() or workspace.status is WorkspaceStatus.CLOSED:
            return
        effect_id = f"cleanup:{phase.value}"
        value = await self.effects.run(
            proposal_id=proposal_id,
            effect_id=effect_id,
            effect_kind="cleanup",
            input_value={"phase": phase.value, "run_id": run_id},
            operation=lambda: workspace.finalize(phase=phase),
            artifact_refs=("artifacts/candidate.patch",),
        )
        if value[0] or (self._proposal_dir(proposal_id) / "artifacts" / "candidate.patch").exists():
            await self._record_artifact(proposal_id, "candidate_patch", self._proposal_dir(proposal_id) / "artifacts" / "candidate.patch", relative="artifacts/candidate.patch")

    async def _abort(
        self,
        proposal_id: str,
        reason: str | BaseException,
        *,
        reason_code: str,
        reason_context: str = "proposal abort",
    ) -> None:
        projection = self._projection(proposal_id)
        if projection.aggregate.is_terminal:
            return
        normalized_reason = nonempty_reason(reason, context=reason_context)
        await self._append_generic(
            "algedonic_raised",
            proposal_id,
            {
                "kind": "pain",
                "reason": normalized_reason,
                "reason_code": reason_code,
                "source": "selfdev-controller",
            },
        )
        await self._append_generic(
            "algedonic_human_notification",
            proposal_id,
            {
                "reason": normalized_reason,
                "reason_code": reason_code,
                "source": "selfdev-controller",
            },
        )
        try:
            await self._cleanup_workspace(proposal_id, phase=ProposalPhase.ABORTED)
        except Exception as exc:
            await self._pause_for_recovery(
                proposal_id,
                (("cleanup", "terminal"),),
                reason=exception_reason(exc, context="abort cleanup"),
            )
            return
        await self._transition(
            proposal_id,
            ProposalPhase.ABORTED,
            reason_code=reason_code if reason_code in {"aborted", "repair_exhausted", "audit_failed", "human_timeout"} else "aborted",
            reason=normalized_reason,
        )

    async def quota_wait(self, proposal_id: str, *, pool_id: str, reset_at: datetime, reason: str) -> None:
        self._require_started()
        if reset_at.tzinfo is None:
            raise ValueError("quota reset_at は timezone-aware でなければなりません")
        source = await self._append_generic("quota_pool_opened", proposal_id, {"pool_id": pool_id, "reset_at": self.clock.now_iso()})
        await self.store.append(
            "proposal_pause_changed",
            {
                "proposal_id": proposal_id,
                "action": "added",
                "pause_id": f"pause-quota-{source.event_id}",
                "cause": "QUOTA_WAIT",
                "actor_type": "node",
                "actor_id": "quota",
                "pool_id": pool_id,
                "reset_at": format_iso_ms(reset_at),
                "source_event_id": source.event_id,
                "reason": nonempty_reason(reason, context="quota wait"),
            },
            proposal_id=proposal_id,
        )

    async def resume_quota(self, proposal_id: str, *, pool_id: str) -> None:
        self._require_started()
        projection = self._projection(proposal_id)
        matches = [cause for cause in projection.aggregate.pause_causes if cause.kind is PauseKind.QUOTA_WAIT and cause.pool_id == pool_id]
        if len(matches) != 1:
            raise ControllerError("指定された quota pause は一意に存在しません")
        source = await self._append_generic("quota_pool_closed", proposal_id, {"pool_id": pool_id})
        await self.store.append(
            "proposal_pause_changed",
            {
                "proposal_id": proposal_id,
                "action": "removed",
                "pause_id": matches[0].pause_id,
                "cause": "QUOTA_WAIT",
                "actor_type": "controller",
                "actor_id": "controller",
                "pool_id": pool_id,
                "reset_at": None,
                "source_event_id": source.event_id,
                "reason": "quota pool が復帰しました",
            },
            proposal_id=proposal_id,
        )

    async def suspend(self, proposal_id: str, *, reason: str) -> None:
        self._require_started()
        normalized_reason = nonempty_reason(reason, context="suspend")
        source = await self._append_generic("algedonic_human_notification", proposal_id, {"reason": normalized_reason})
        await self.store.append(
            "proposal_pause_changed",
            {
                "proposal_id": proposal_id,
                "action": "added",
                "pause_id": f"pause-suspend-{source.event_id}",
                "cause": "SUSPEND",
                "actor_type": "human",
                "actor_id": "human",
                "pool_id": None,
                "reset_at": None,
                "source_event_id": source.event_id,
                "reason": normalized_reason,
            },
            proposal_id=proposal_id,
        )

    async def resume_suspend(self, proposal_id: str, *, pause_id: str) -> None:
        self._require_started()
        projection = self._projection(proposal_id)
        cause = next((item for item in projection.aggregate.pause_causes if item.pause_id == pause_id and item.kind is PauseKind.SUSPEND), None)
        if cause is None:
            raise ControllerError("指定された SUSPEND pause はありません")
        source = await self._append_generic("instruction_completed", proposal_id, {"pause_id": pause_id, "reason": "resume"})
        await self.store.append(
            "proposal_pause_changed",
            {
                "proposal_id": proposal_id,
                "action": "removed",
                "pause_id": pause_id,
                "cause": "SUSPEND",
                "actor_type": "human",
                "actor_id": "human",
                "pool_id": None,
                "reset_at": None,
                "source_event_id": source.event_id,
                "reason": "SUSPEND を解除しました",
            },
            proposal_id=proposal_id,
        )

    async def abort(self, proposal_id: str, *, reason: str) -> None:
        self._require_started()
        await self._abort(proposal_id, reason, reason_code="aborted")

    async def record_merge_outcome(self, proposal_id: str, *, merged: bool, reason: str = "", merge_sha: str | None = None) -> None:
        self._require_started()
        projection = self._projection(proposal_id)
        if projection.aggregate.phase is not ProposalPhase.MERGE_READY:
            raise ControllerError("MERGE_READY 以外の Proposal に merge outcome は記録できません")
        if not merged and not reason.strip():
            raise ValueError("Human 却下には reason が必要です")
        event = await self._append_generic(
            "policy_decision",
            proposal_id,
            {"decision": "merge" if merged else "archive", "reason": reason or "merged", "merge_sha": merge_sha},
            actor_type="human",
        )
        await self._transition(
            proposal_id,
            ProposalPhase.DONE if merged else ProposalPhase.ARCHIVED,
            reason_code="merged" if merged else "archived",
            reason=reason or "Human が merge outcome を記録しました",
            decision_event_id=event.event_id,
        )

    async def _contain_proposal_failure(
        self,
        proposal_id: str,
        phase: ProposalPhase,
        exc: Exception,
    ) -> None:
        """Proposal処理の失敗をABORT+algedonicへ封じ込める。"""

        await self._abort(
            proposal_id,
            exc,
            reason_code="aborted",
            reason_context=f"{phase.value} proposal processing",
        )

    async def step(self, proposal_id: str | None = None) -> bool:
        self._require_started()
        active_id = proposal_id
        if active_id is None:
            active = [item for item in self.store.replay().values() if not item.aggregate.is_terminal]
            if not active:
                return False
            if len(active) != 1:
                raise ControllerError("非terminal Proposal が複数あります")
            active_id = active[0].proposal_id
        projection = self._projection(active_id)
        if projection.aggregate.is_terminal:
            return False
        if projection.integrity_failed:
            return False
        if projection.aggregate.is_paused:
            raise ControllerPaused(f"Proposal は pause 中です: {active_id}")
        proposal = self._manifest(active_id)
        phase = projection.aggregate.phase
        try:
            if phase is ProposalPhase.PROPOSED:
                await self._transition(active_id, ProposalPhase.CONSORTIUM_REVIEW, reason_code="review_started", reason="initial Consortium review を開始しました")
            elif phase is ProposalPhase.CONSORTIUM_REVIEW:
                await self._run_initial_review(proposal)
            elif phase is ProposalPhase.NEEDS_HUMAN:
                await self._handle_needs_human(proposal)
                return self._projection(active_id).aggregate.phase is not ProposalPhase.NEEDS_HUMAN
            elif phase is ProposalPhase.APPROVED:
                await self._handle_approved(proposal)
            elif phase is ProposalPhase.WORKSPACE_READY:
                await self._handle_workspace_ready(proposal)
            elif phase is ProposalPhase.IMPLEMENTING:
                await self._transition(active_id, ProposalPhase.GATES_RUNNING, reason_code="implementation_completed", reason="implementation Run の成果物を gate に渡します")
            elif phase is ProposalPhase.GATES_RUNNING:
                await self._handle_gates_running(proposal)
            elif phase is ProposalPhase.GATES_FAILED:
                await self._handle_gates_failed(proposal)
            elif phase is ProposalPhase.GATES_PASSED:
                await self._handle_gates_passed(proposal)
            elif phase is ProposalPhase.AUDIT:
                await self._handle_audit(proposal)
            elif phase is ProposalPhase.FINAL_CONSORTIUM:
                await self._handle_final_consortium(proposal)
            elif phase is ProposalPhase.MERGE_READY:
                return False
            else:
                raise ControllerError(f"未処理の Proposal phase です: {phase.value}")
        except Exception as exc:
            await self._contain_proposal_failure(active_id, phase, exc)
        return True

    async def run_once(self) -> bool:
        return await self.step()

    async def run_forever(self, *, stop_event: asyncio.Event, idle_seconds: float = 0.1) -> None:
        self._require_started()
        if idle_seconds <= 0:
            raise ValueError("idle_seconds は正数でなければなりません")
        while not stop_event.is_set():
            progressed = await self.step()
            if not progressed:
                await asyncio.sleep(idle_seconds)


__all__ = [
    "ControllerError",
    "ControllerPaused",
    "BackendInvocationTimeout",
    "ImplementationRunTimeout",
    "ImplementationResult",
    "QuotaWait",
    "SelfDevController",
]
