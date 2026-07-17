"""Wave 3 headless controller / consortium / audit / scheduler tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from vsm.agents import AgentRequest, extract_json_object, invoke_with_json_retry
from vsm.agents.backends.fake import FakeAgentRuntime
from vsm.clock import FakeClock
from vsm.roles import SystemRole
from vsm.selfdev.audit import S3StarAuditRunner
from vsm.selfdev.consortium_adapter import (
    ConsortiumAdapterError,
    DurableHumanWaiter,
    HumanTimeoutPolicy,
    SelfDevConsortiumAdapter,
)
from vsm.selfdev.controller import (
    BackendInvocationTimeout,
    ImplementationResult,
    SelfDevController,
    _RuntimeImplementationRunner,
)
from vsm.selfdev.service import SelfDevService
from vsm.selfdev.state_machine import ProposalPhase
from vsm.selfdev.git import candidate_diff_sha256, git_output
from vsm.selfdev.models import (
    AcceptanceCriterion,
    ActorRef,
    BudgetEstimate,
    GateReport,
    GateResult,
    PathRule,
    ProposalManifest,
    QuotaEstimate,
    ReadyQueueOrigin,
    RunRuntime,
)
from vsm.selfdev.ready_queue import scope_overlaps
from vsm.selfdev.scheduler import SelfDevScheduler
from vsm.selfdev.store import SelfDevEventStore
from vsm.selfdev.verification import scope_sha256
from vsm.runtime.manifest import RunManifest


def _manifest(*, proposal_id: str | None = None, risk: str = "low", path: str = "candidate.txt") -> ProposalManifest:
    proposal_id = proposal_id or f"proposal-{'a' * 32}"
    return ProposalManifest(
        id=proposal_id,
        title="Wave 3 candidate",
        motivation="controller の headless loop を検証する",
        scope=(PathRule(path=path, kind="file"),),
        acceptance_criteria=(
            AcceptanceCriterion(
                id="AC-1",
                statement="candidate file exists",
                verifier={"kind": "path_exists", "path": path},
            ),
        ),
        risk_class=risk,
        budget_estimate=BudgetEstimate(tokens=100, active_wall_clock_seconds=60),
        origin=ReadyQueueOrigin(kind="ready_queue", decision_ref="test", roadmap_ref="ROADMAP.md"),
        created_at="2026-07-13T00:00:00.000Z",
        created_by=ActorRef(actor_type="scheduler", actor_id="test"),
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Wave 3 Test")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


class _Implementation:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    async def run(self, *, manifest, worktree: Path, resume: bool):
        self.calls.append((manifest.attempt, resume))
        (worktree / "candidate.txt").write_text("implemented\n", encoding="utf-8")
        return ImplementationResult(tokens=12, active_wall_clock_seconds=1)


class _EmptyTimeoutImplementation:
    async def run(self, *, manifest, worktree: Path, resume: bool):
        (worktree / "candidate.txt").write_text("candidate before timeout\n", encoding="utf-8")
        raise TimeoutError()


class _BackendTimeoutRuntime:
    timeout_seconds = 900.0

    async def invoke(self, request: AgentRequest):
        raise TimeoutError()


class _RecordingTimeoutRuntime:
    timeout_seconds = 900.0

    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    async def invoke(self, request: AgentRequest):
        self.requests.append(request)
        raise TimeoutError()


class _Gates:
    def __init__(self, *, fail_attempts: set[int] | None = None) -> None:
        self.calls: list[int] = []
        self.fail_attempts = fail_attempts or set()

    async def run(self, *, manifest, worktree: Path, output_dir: Path):
        self.calls.append(manifest.attempt)
        output_dir.mkdir(parents=True, exist_ok=True)
        log_dir = output_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        gates: dict[str, GateResult] = {}
        for name in ("g1", "g2", "g3", "g4"):
            path = log_dir / f"{name}.log"
            path.write_text(f"{name}\n", encoding="utf-8")
            gates[name] = GateResult(
                status="fail" if manifest.attempt in self.fail_attempts and name == "g1" else "pass",
                duration_ms=1,
                summary="test",
                log_ref=f"gates/attempt-{manifest.attempt}/logs/{name}.log",
                log_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        return GateReport(
            proposal_id=manifest.proposal_id,
            implementation_run_id=manifest.run_id,
            gate_attempt=manifest.attempt,
            generated_at="2026-07-13T00:00:00.000Z",
            worktree_path=str(worktree),
            report_ref=f"gates/attempt-{manifest.attempt}/gate_report.json",
            base_sha=manifest.base_sha,
            scope_sha256=manifest.scope_sha256,
            candidate_diff_sha256=candidate_diff_sha256(worktree, manifest.base_sha),
            gates_requested=("g1", "g2", "g3", "g4"),
            status="fail" if manifest.attempt in self.fail_attempts else "pass",
            exit_code=1 if manifest.attempt in self.fail_attempts else 0,
            changed_paths=("candidate.txt",),
            gates=gates,
        )


def _consortium_runtimes(*, final_decision: str = "MERGE_READY") -> dict[SystemRole, FakeAgentRuntime]:
    def statement(_: AgentRequest) -> str:
        return json.dumps({"statement": "観点から問題なし"}, ensure_ascii=False)

    def s5(request: AgentRequest) -> str:
        if "synthesize" not in request.prompt:
            return json.dumps({"statement": "方針上問題なし"}, ensure_ascii=False)
        if "final" in request.prompt:
            return json.dumps(
                {
                    "decision": final_decision,
                    "reason": "監査済みである",
                    "dissent_summary": "なし",
                    "conditions": [],
                    "residual_risks": ["human merge が必要"],
                    "merge_recommendation_reason": "gate と audit が pass",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "decision": "APPROVE",
                "reason": "実装可能である",
                "dissent_summary": "なし",
                "conditions": ["G1-G4 を実行する"],
                "residual_risks": [],
                "merge_recommendation_reason": None,
            },
            ensure_ascii=False,
        )

    return {
        SystemRole.S3_ALLOCATOR: FakeAgentRuntime(response=statement),
        SystemRole.S4_SCANNER: FakeAgentRuntime(response=statement),
        SystemRole.S5_POLICY: FakeAgentRuntime(response=s5),
    }


class _ImmediateHumanWaiter:
    def __init__(self, store: SelfDevEventStore) -> None:
        self.store = store

    async def wait(self, *, proposal_id, consortium_id, review_id, risk_class, deadline):
        await self.store.append(
            "human_review_requested",
            {
                "proposal_id": proposal_id,
                "consortium_id": consortium_id,
                "review_id": review_id,
                "review_kind": "initial",
                "risk_class": risk_class,
                "deadline": deadline.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "approval_required": True,
            },
            proposal_id=proposal_id,
            schema_version=2,
        )
        return "protected review statement"

    async def respond(self, *, proposal_id, consortium_id, review_id, decision, response):
        return await self.store.append(
            "human_review_responded",
            {
                "proposal_id": proposal_id,
                "consortium_id": consortium_id,
                "review_id": review_id,
                "decision": decision,
                "response": response,
                "response_sha256": hashlib.sha256(response.encode()).hexdigest(),
            },
            proposal_id=proposal_id,
            actor_type="human",
            schema_version=2,
        )


def _audit_runtime() -> FakeAgentRuntime:
    response = {
        "acceptance_results": [
            {"criterion_id": "AC-1", "status": "pass", "evidence_refs": ["artifacts/candidate.patch"], "finding": "ok"}
        ],
        "findings": [],
        "verdict": "pass",
        "summary": "受入条件と証拠を突合した",
    }
    return FakeAgentRuntime(response=json.dumps(response), session_ref="audit-session")


def test_extract_json_object_accepts_prefix_and_code_fence() -> None:
    text = '監査結果です。\n```json\n{"statement":"実施可能です"}\n```\n以上です。'

    assert extract_json_object(text) == {"statement": "実施可能です"}


@pytest.mark.asyncio
async def test_selfdev_statement_retries_once_and_saves_raw_responses(tmp_path: Path) -> None:
    clock = FakeClock()
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=clock)
    await store.start()
    calls: list[AgentRequest] = []

    def malformed_then_fenced(request: AgentRequest) -> str:
        calls.append(request)
        if len(calls) == 1:
            return "statement は次のとおりです: {broken"
        return '説明を除けば次です。\n```json\n{"statement":"再質問で成功"}\n```'

    runtimes = _consortium_runtimes()
    runtimes[SystemRole.S3_ALLOCATOR] = FakeAgentRuntime(response=malformed_then_fenced)
    adapter = SelfDevConsortiumAdapter(
        store=store,
        runtimes=runtimes,
        clock=clock,
        timeout_policy=HumanTimeoutPolicy(
            timeout_seconds={"low": 1, "normal": 1, "protected": 1},
            timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
        ),
    )
    proposal = _manifest(proposal_id=f"proposal-{'a' * 32}")
    try:
        await adapter.convene(
            proposal=proposal,
            consortium_id="consortium-json-retry",
            review_kind="initial",
            dossier={"proposal": proposal.canonical_dict()},
            dossier_ref="artifacts/initial-dossier.json",
            human=False,
        )
        assert len(calls) == 3
        assert '期待スキーマは {"statement": "string"}' in calls[0].prompt
        assert "statement 本文は日本語ファースト" in calls[0].prompt
        assert "英語だけにしないでください" in calls[0].prompt
        assert "コードフェンス、前置き、後置きは禁止" in calls[0].prompt
        assert "パースエラー:" in calls[1].prompt
        raw_dir = tmp_path / "runs" / "selfdev" / "proposals" / proposal.id / "artifacts"
        assert (raw_dir / "raw-statement-S3_ALLOCATOR-1.txt").read_text(encoding="utf-8") == "statement は次のとおりです: {broken"
        assert "再質問で成功" in (raw_dir / "raw-statement-S3_ALLOCATOR-1-retry.txt").read_text(encoding="utf-8")
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_json_retry_fails_after_exactly_two_invalid_responses() -> None:
    runtime = FakeAgentRuntime(response="説明だけでJSONなし")

    with pytest.raises(ConsortiumAdapterError, match="JSON object"):
        await invoke_with_json_retry(
            lambda prompt: runtime.invoke(AgentRequest(prompt=prompt)),
            "JSONを返してください",
            SelfDevConsortiumAdapter._parse_statement,
        )

    assert len(runtime.invocations) == 2
    assert "パースエラー:" in runtime.invocations[1].prompt


@pytest.mark.asyncio
async def test_wave3_fake_runtime_reaches_merge_ready_without_push_or_merge(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    clock = FakeClock()
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=clock)
    implementation = _Implementation()
    gates = _Gates()
    controller = SelfDevController(
        repository=repository,
        store=store,
        writer_runtime=RunRuntime(role="S1_WORKER", backend="fake", model="fake", reasoning_effort="standard"),
        implementation_runner=implementation,
        gate_runner=gates,
        audit_runner=S3StarAuditRunner(runtime=_audit_runtime(), clock=clock),
        consortium_runtimes=_consortium_runtimes(),
        timeout_policy=HumanTimeoutPolicy(
            timeout_seconds={"low": 0.01, "normal": 0.01, "protected": 0.01},
            timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
        ),
        worktree_root=tmp_path / "worktrees",
        base_ref="main",
        clock=clock,
    )
    await controller.start()
    try:
        proposal = _manifest()
        await controller.submit_proposal(proposal)
        for _ in range(20):
            await controller.step()
            if controller._projection(proposal.id).aggregate.phase.value == "MERGE_READY":
                break
        assert controller._projection(proposal.id).aggregate.phase.value == "MERGE_READY"
        assert implementation.calls == [(1, False)]
        assert gates.calls == [1]
        assert (tmp_path / "runs" / "selfdev" / "proposals" / proposal.id / "pr-description.md").exists()
        assert not (tmp_path / "worktrees" / proposal.id).exists()
        assert git_output(repository, "status", "--porcelain") == ""
        assert git_output(repository, "branch", "--list", f"selfdev/{proposal.id}").strip() == f"selfdev/{proposal.id}"
        assert not any(event.event_type in {"git_push", "git_merge"} for event in store.read_events())
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_wave3_repair_is_limited_to_one_attempt_and_aborts(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    clock = FakeClock()
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=clock)
    gates = _Gates(fail_attempts={1, 2})
    controller = SelfDevController(
        repository=repository,
        store=store,
        writer_runtime=RunRuntime(role="S1_WORKER", backend="fake", model="fake", reasoning_effort="standard"),
        implementation_runner=_Implementation(),
        gate_runner=gates,
        audit_runner=S3StarAuditRunner(runtime=_audit_runtime(), clock=clock),
        consortium_runtimes=_consortium_runtimes(),
        timeout_policy=HumanTimeoutPolicy(
            timeout_seconds={"low": 0.01, "normal": 0.01, "protected": 0.01},
            timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
        ),
        worktree_root=tmp_path / "worktrees",
        base_ref="main",
        clock=clock,
    )
    await controller.start()
    try:
        proposal = _manifest(proposal_id=f"proposal-{'b' * 32}")
        await controller.submit_proposal(proposal)
        for _ in range(30):
            await controller.step()
            if controller._projection(proposal.id).aggregate.is_terminal:
                break
        projection = controller._projection(proposal.id)
        assert projection.aggregate.phase.value == "ABORTED"
        assert projection.aggregate.repair_used is True
        assert gates.calls == [1, 2]
        assert (tmp_path / "runs" / "selfdev" / "proposals" / proposal.id / "artifacts" / "candidate.patch").exists()
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_empty_timeout_reason_is_preserved_in_tool_failure_and_abort(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=FakeClock())
    controller = SelfDevController(
        repository=repository,
        store=store,
        writer_runtime=RunRuntime(role="S1_WORKER", backend="fake", model="fake", reasoning_effort="standard"),
        implementation_runner=_EmptyTimeoutImplementation(),
        gate_runner=_Gates(),
        audit_runner=S3StarAuditRunner(runtime=_audit_runtime(), clock=FakeClock()),
        consortium_runtimes=_consortium_runtimes(),
        timeout_policy=HumanTimeoutPolicy(
            timeout_seconds={"low": 1, "normal": 1, "protected": 1},
            timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
        ),
        worktree_root=tmp_path / "worktrees",
        base_ref="main",
        clock=FakeClock(),
        implementation_timeout_margin_seconds=0,
    )
    await controller.start()
    try:
        proposal = _manifest(proposal_id=f"proposal-{'f' * 32}")
        await controller.submit_proposal(proposal)
        for _ in range(4):
            await controller.step()
            if controller._projection(proposal.id).aggregate.is_terminal:
                break

        assert controller._projection(proposal.id).aggregate.phase is ProposalPhase.ABORTED
        events = controller._events(proposal.id)
        failed = next(event for event in events if event.event_type == "tool_failed")
        aborted = next(
            event
            for event in events
            if event.event_type == "proposal_state_changed"
            and event.payload["to_state"] == "ABORTED"
        )
        assert failed.payload["reason"] == "TimeoutError (implementation run)"
        assert aborted.payload["reason"] == "TimeoutError (implementation run)"
        assert any(event.event_type == "algedonic_raised" for event in events)
        assert (
            tmp_path / "runs" / "selfdev" / "proposals" / proposal.id / "artifacts" / "candidate.patch"
        ).exists()
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_proposal_failure_does_not_kill_controller_and_next_proposal_runs(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=FakeClock())
    controller = SelfDevController(
        repository=repository,
        store=store,
        writer_runtime=RunRuntime(role="S1_WORKER", backend="fake", model="fake", reasoning_effort="standard"),
        implementation_runner=_EmptyTimeoutImplementation(),
        gate_runner=_Gates(),
        audit_runner=S3StarAuditRunner(runtime=_audit_runtime(), clock=FakeClock()),
        consortium_runtimes=_consortium_runtimes(),
        timeout_policy=HumanTimeoutPolicy(
            timeout_seconds={"low": 1, "normal": 1, "protected": 1},
            timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
        ),
        worktree_root=tmp_path / "worktrees",
        base_ref="main",
        clock=FakeClock(),
        implementation_timeout_margin_seconds=0,
    )
    service = SelfDevService(controller, idle_seconds=0.001)
    await service.start()
    try:
        first = _manifest(proposal_id=f"proposal-{'1' * 32}")
        await controller.submit_proposal(first)
        for _ in range(200):
            if controller._projection(first.id).aggregate.is_terminal:
                break
            await asyncio.sleep(0.001)
        assert controller._projection(first.id).aggregate.phase is ProposalPhase.ABORTED
        assert service.healthy
        assert service.fatal is None

        second = _manifest(proposal_id=f"proposal-{'2' * 32}")
        await controller.submit_proposal(second)
        for _ in range(200):
            if controller._projection(second.id).aggregate.is_terminal:
                break
            await asyncio.sleep(0.001)
        assert controller._projection(second.id).aggregate.phase is ProposalPhase.ABORTED
        assert service.healthy
        assert service.fatal is None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_cleanup_failure_pauses_proposal_and_controller_skips_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository, _ = _repository(tmp_path)
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=FakeClock())
    controller = SelfDevController(
        repository=repository,
        store=store,
        writer_runtime=RunRuntime(role="S1_WORKER", backend="fake", model="fake", reasoning_effort="standard"),
        implementation_runner=_Implementation(),
        gate_runner=_Gates(),
        audit_runner=S3StarAuditRunner(runtime=_audit_runtime(), clock=FakeClock()),
        consortium_runtimes=_consortium_runtimes(),
        timeout_policy=HumanTimeoutPolicy(
            timeout_seconds={"low": 1, "normal": 1, "protected": 1},
            timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
        ),
        worktree_root=tmp_path / "worktrees",
        base_ref="main",
        clock=FakeClock(),
    )
    await controller.start()
    try:
        first = _manifest(proposal_id=f"proposal-{'1' * 32}")
        await controller.submit_proposal(first)

        async def fail_cleanup(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("cleanup failed")

        monkeypatch.setattr(controller, "_cleanup_workspace", fail_cleanup)
        await controller._abort(first.id, "implementation failed", reason_code="aborted")

        projection = controller._projection(first.id)
        assert projection.aggregate.is_paused
        assert any(event.event_type == "algedonic_raised" for event in controller._events(first.id))

        service = SelfDevService(controller, idle_seconds=0.001)
        await service.start()
        try:
            assert await controller.step() is False
            await asyncio.sleep(0.01)
            assert service.healthy
            assert service.fatal is None
        finally:
            await service.stop()
    finally:
        await controller.stop()


def test_implementation_timeout_is_derived_from_proposal_budget_and_margin(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=FakeClock())
    controller = SelfDevController(
        repository=repository,
        store=store,
        writer_runtime=RunRuntime(role="S1_WORKER", backend="fake", model="fake", reasoning_effort="standard"),
        implementation_runner=_Implementation(),
        gate_runner=_Gates(),
        audit_runner=S3StarAuditRunner(runtime=_audit_runtime(), clock=FakeClock()),
        consortium_runtimes=_consortium_runtimes(),
        timeout_policy=HumanTimeoutPolicy(
            timeout_seconds={"low": 1, "normal": 1, "protected": 1},
            timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
        ),
        worktree_root=tmp_path / "worktrees",
        base_ref="main",
        clock=FakeClock(),
        implementation_timeout_margin_seconds=17.5,
    )
    proposal = _manifest(proposal_id=f"proposal-{'3' * 32}").model_copy(
        update={"budget_estimate": BudgetEstimate(tokens=100, active_wall_clock_seconds=1800)}
    )
    assert controller.implementation_timeout_seconds(proposal) == 1817.5


@pytest.mark.asyncio
async def test_backend_timeout_is_labeled_as_single_invocation_timer(tmp_path: Path) -> None:
    with pytest.raises(BackendInvocationTimeout, match="900 seconds"):
        await _RuntimeImplementationRunner(_BackendTimeoutRuntime()).run(
            manifest=object(),  # type: ignore[arg-type]
            worktree=tmp_path,
            resume=False,
        )


@pytest.mark.asyncio
async def test_backend_invocation_timeout_uses_run_budget_and_explains_explicit_short_setting(
    tmp_path: Path,
) -> None:
    manifest = RunManifest(
        run_id="run-timeout",
        repository=tmp_path,
        base_sha="base",
        worktree_path=tmp_path,
        backend="fake",
        model="fake",
        budget={"active_wall_clock_seconds": 1800},
        risk_class="normal",
        issued_by={"decision": "test", "conversation": "test"},
    )
    runtime = _RecordingTimeoutRuntime()
    runner = _RuntimeImplementationRunner(
        runtime,
        implementation_timeout_margin_seconds=300,
    )
    with pytest.raises(BackendInvocationTimeout, match="2100 seconds"):
        await runner.run(manifest=manifest, worktree=tmp_path, resume=False)
    assert runtime.requests[0].timeout_seconds == 2100

    explicit_runtime = _RecordingTimeoutRuntime()
    explicit_runner = _RuntimeImplementationRunner(
        explicit_runtime,
        implementation_timeout_margin_seconds=300,
        configured_timeout_seconds=900,
        configured_timeout_explicit=True,
    )
    with pytest.raises(BackendInvocationTimeout, match="明示設定された backend timeout"):
        await explicit_runner.run(manifest=manifest, worktree=tmp_path, resume=False)
    assert explicit_runtime.requests[0].timeout_seconds == 900


@pytest.mark.asyncio
async def test_durable_human_waiter_survives_store_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    root = tmp_path / "runs" / "selfdev"
    first = SelfDevEventStore(root, clock=clock)
    await first.start()
    try:
        proposal = _manifest(proposal_id=f"proposal-{'c' * 32}")
        first.layout.write_proposal_manifest(proposal)
        await first.append(
            "proposal_state_changed",
            {
                "proposal_id": proposal.id,
                "from_state": None,
                "to_state": "PROPOSED",
                "reason_code": "proposal_created",
                "reason": "test",
                "related_run_id": None,
                "decision_event_id": None,
                "artifact_refs": (),
            },
            proposal_id=proposal.id,
        )
        waiter = DurableHumanWaiter(first, clock=clock, poll_seconds=0.01)
        deadline = clock.now() + timedelta(seconds=30)
        task = asyncio.create_task(
            waiter.wait(
                proposal_id=proposal.id,
                consortium_id="consortium-c",
                review_id="review-consortium-c",
                risk_class="protected",
                deadline=deadline,
            )
        )
        await asyncio.sleep(0.02)
        await first.stop()
        second = SelfDevEventStore(root, clock=clock)
        await second.start()
        try:
            restored_waiter = DurableHumanWaiter(second, clock=clock, poll_seconds=0.01)
            await restored_waiter.respond(
                proposal_id=proposal.id,
                consortium_id="consortium-c",
                review_id="review-consortium-c",
                decision="approve",
                response="事前承認",
            )
            assert await task == "事前承認"
            requests = [event for event in second.read_events() if event.event_type == "human_review_requested"]
            assert len(requests) == 1
        finally:
            await second.stop()
    finally:
        if first._writer is not None:  # pragma: no cover - defensive cleanup
            await first.stop()


@pytest.mark.asyncio
async def test_protected_proposal_cannot_be_approved_before_explicit_human_approval(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    clock = FakeClock()
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=clock)
    waiter = _ImmediateHumanWaiter(store)
    adapter = SelfDevConsortiumAdapter(
        store=store,
        runtimes=_consortium_runtimes(),
        clock=clock,
        human_waiter=waiter,
        timeout_policy=HumanTimeoutPolicy(
            timeout_seconds={"low": 1, "normal": 1, "protected": 1},
            timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
        ),
    )
    controller = SelfDevController(
        repository=repository,
        store=store,
        writer_runtime=RunRuntime(role="S1_WORKER", backend="fake", model="fake", reasoning_effort="standard"),
        implementation_runner=_Implementation(),
        gate_runner=_Gates(),
        audit_runner=S3StarAuditRunner(runtime=_audit_runtime(), clock=clock),
        consortium=adapter,
        worktree_root=tmp_path / "worktrees",
        base_ref="main",
        clock=clock,
    )
    await controller.start()
    try:
        proposal = _manifest(
            proposal_id=f"proposal-{'9' * 32}",
            risk="protected",
            path=".github/config.yml",
        )
        await controller.submit_proposal(proposal)
        await controller.step()
        await controller.step()
        assert controller._projection(proposal.id).aggregate.phase.value == "NEEDS_HUMAN"
        await controller.respond_human(proposal.id, decision="approve", response="明示承認")
        await controller.step()
        assert controller._projection(proposal.id).aggregate.phase.value == "APPROVED"
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_normal_human_timeout_aborts_consensus(tmp_path: Path) -> None:
    clock = FakeClock()
    store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=clock)
    await store.start()
    try:
        proposal = _manifest(proposal_id=f"proposal-{'8' * 32}", risk="normal")
        store.layout.write_proposal_manifest(proposal)
        adapter = SelfDevConsortiumAdapter(
            store=store,
            runtimes=_consortium_runtimes(),
            clock=clock,
            timeout_policy=HumanTimeoutPolicy(
                timeout_seconds={"low": 0.01, "normal": 0.01, "protected": 0.01},
                timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
            ),
        )
        with pytest.raises(Exception, match="timeout"):
            await adapter.convene(
                proposal=proposal,
                consortium_id="consortium-normal-timeout",
                review_kind="initial",
                dossier={"proposal": proposal.canonical_dict()},
                dossier_ref="artifacts/dossier.json",
                human=True,
            )
        assert any(event.event_type == "consortium_aborted" for event in store.read_events())
    finally:
        await store.stop()


@pytest.mark.asyncio
async def test_negative_audit_is_valid_report_for_final_review(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    proposal = _manifest(proposal_id=f"proposal-{'7' * 32}")
    worktree = tmp_path / "candidate"
    _git(repository, "worktree", "add", "-b", "audit-candidate", str(worktree), base_sha)
    try:
        (worktree / "candidate.txt").write_text("candidate\n", encoding="utf-8")
        commit_sha = _git(worktree, "rev-parse", "HEAD")
        candidate = type("Candidate", (), {
            "proposal_id": proposal.id,
            "commit_sha": commit_sha,
            "parent_sha": base_sha,
            "tree_sha": _git(worktree, "rev-parse", "HEAD^{tree}"),
            "branch": "audit-candidate",
            "base_sha": base_sha,
            "diff_sha256": candidate_diff_sha256(worktree, base_sha),
            "to_dict": lambda self: {
                "proposal_id": self.proposal_id,
                "commit_sha": self.commit_sha,
                "parent_sha": self.parent_sha,
                "tree_sha": self.tree_sha,
                "branch": self.branch,
                "base_sha": self.base_sha,
                "diff_sha256": self.diff_sha256,
            },
        })()
        store = SelfDevEventStore(tmp_path / "runs" / "selfdev", clock=FakeClock())
        runtime = FakeAgentRuntime(
            response=(
                "監査結果です。\n```json\n"
                + json.dumps({
                    "acceptance_results": [{"criterion_id": "AC-1", "status": "fail", "finding": "not proven"}],
                    "findings": [{"finding_id": "F-1", "severity": "error", "category": "acceptance", "summary": "criterion failed"}],
                    "verdict": "fail",
                    "summary": "negative but valid",
                })
                + "\n```\n以上です。"
            ),
            session_ref="audit-session",
        )
        audit = S3StarAuditRunner(
            runtime=runtime,
            clock=FakeClock(),
        )
        proposal_path = tmp_path / "proposal.json"
        proposal_path.write_text(proposal.canonical_json(), encoding="utf-8")
        gate_path = tmp_path / "gate.json"
        gate = _Gates()
        gate_report = await gate.run(
            manifest=type("Manifest", (), {
                "proposal_id": proposal.id,
                "run_id": "run-audit",
                "attempt": 1,
                "base_sha": base_sha,
                "scope_sha256": scope_sha256([rule.model_dump(mode="json") for rule in proposal.scope]),
                "scope": proposal.scope,
            })(),
            worktree=worktree,
            output_dir=tmp_path / "gates",
        )
        gate_path.write_text(gate_report.model_dump_json(), encoding="utf-8")
        diff_path = tmp_path / "candidate.patch"
        diff_path.write_text("patch\n", encoding="utf-8")
        report = await audit.run(
            proposal=proposal,
            candidate=candidate,
            gate_report=gate_report,
            root=tmp_path,
            proposal_manifest_ref="proposal.json",
            manifest_path=proposal_path,
            gate_report_ref="gate.json",
            gate_report_path=gate_path,
            diff_ref="candidate.patch",
            diff_path=diff_path,
            changed_paths=("candidate.txt",),
            protected_approval=None,
            budget_actual={},
            audit_id="audit-negative",
        )
        assert report.verdict == "fail"
        assert report.findings[0].severity == "error"
        assert runtime.invocations[0].session_ref is None
        assert report.auditor.session_ref is None
        assert (tmp_path / "artifacts" / "raw-audit-audit-negative.txt").exists()
    finally:
        _git(repository, "worktree", "remove", "--force", str(worktree))


def test_wave3_scheduler_enforces_single_slot_reserve_dependency_and_path_conflict() -> None:
    quota = BudgetEstimate(
        tokens=100,
        active_wall_clock_seconds=60,
        pool_quota=(QuotaEstimate(pool_id="pool", unit="tokens", amount=5),),
    )
    first = _manifest(proposal_id=f"proposal-{'d' * 32}").model_copy(update={"budget_estimate": quota})
    second = _manifest(proposal_id=f"proposal-{'e' * 32}", path="other.txt").model_copy(update={"budget_estimate": quota})
    scheduler = SelfDevScheduler([first, second])
    blocked = scheduler.decide(active=None, done_ids=(), remaining={"pool": 10}, reserve={"pool": 5})
    assert blocked.proposal is None
    assert "不足" in (blocked.admission.reason or "")
    assert scope_overlaps(first, first)
    active = scheduler.decide(active=first, done_ids=(), remaining={"pool": 10}, reserve={"pool": 5})
    assert active.proposal is None
    assert "slot" in (active.admission.reason or "")


def test_wave3_scheduler_skips_paused_candidates() -> None:
    first = _manifest(proposal_id=f"proposal-{'d' * 32}")
    second = _manifest(proposal_id=f"proposal-{'e' * 32}", path="other.txt")
    scheduler = SelfDevScheduler([first, second])

    decision = scheduler.decide(
        active=None,
        done_ids=(),
        remaining={},
        reserve={},
        paused_ids=(first.id,),
    )

    assert decision.proposal is second


def test_wave3_consortium_decision_extended_fields_are_strict() -> None:
    from vsm.selfdev.models import ConsortiumDecision

    decision = ConsortiumDecision(
        consortium_id="consortium-test",
        proposal_id=f"proposal-{'f' * 32}",
        review_kind="final",
        decision="MERGE_READY",
        reason="ok",
        dissent_summary="minor concern",
        conditions=("human merge",),
        residual_risks=("drift",),
        merge_recommendation_reason="all evidence is present",
        dossier_ref="artifacts/final-dossier.json",
        dossier_sha256="a" * 64,
        human_participated=False,
        human_timed_out=False,
    )
    assert decision.residual_risks == ("drift",)
    with pytest.raises(ValueError):
        ConsortiumDecision(
            consortium_id="consortium-test",
            proposal_id=f"proposal-{'f' * 32}",
            review_kind="final",
            decision="MERGE_READY",
            reason="ok",
            dossier_ref="artifacts/final-dossier.json",
            dossier_sha256="a" * 64,
            human_participated=False,
            human_timed_out=False,
        )
