"""Wave 2 self-development の workspace / gate / candidate commit テスト。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from vsm.agents.backends.fake import FakeAgentRuntime
from vsm.config import AgentsConfig, RunConfig, SelfDevConfig
from vsm.errors import CandidateCommitError, WorkspaceError
from vsm.eventlog.schema import validate_event_payload
from vsm.gates import runner
from vsm.gates.events import record_gate_report_generated
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform
from vsm.runtime.manifest import RunManifest
from vsm.selfdev import git as selfdev_git
from vsm.selfdev.git import candidate_diff_sha256, list_worktrees
from vsm.selfdev.verification import REQUIRED_GATES, scope_sha256
from vsm.selfdev.workspace import ProposalWorkspace, WorkspaceStatus


@pytest.fixture(autouse=True)
def _allow_test_repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    """Docker の root 実行でも一時 repository を Git に認識させる。"""

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")


def _git(cwd: Path, *args: str) -> str:
    environment = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        environment.pop(key, None)
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def _repository(tmp_path: Path) -> tuple[Path, str]:
    """実 Git の base fixture。protected と frontend の判定対象も含める。"""

    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "wave2@example.invalid")
    _git(repository, "config", "user.name", "Nanihold Wave 2 Test")
    (repository / "src").mkdir()
    (repository / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "outside.txt").write_text("outside base\n", encoding="utf-8")
    (repository / "vsm" / "gates").mkdir(parents=True)
    (repository / "vsm" / "gates" / "policy.py").write_text(
        "POLICY = 'base'\n", encoding="utf-8"
    )
    (repository / "frontend").mkdir()
    (repository / "frontend" / "package.json").write_text(
        json.dumps({"scripts": {"lint": "lint", "build": "build"}}),
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD").strip()


def _manifest(
    tmp_path: Path,
    repository: Path,
    base_sha: str,
    *,
    scope: tuple[dict[str, str], ...],
    proposal_hex: str = "a" * 32,
    run_hex: str = "b" * 32,
    risk_class: str = "normal",
    proposal_manifest_sha256: str = "c" * 64,
    required_gates: tuple[str, ...] = ("g1", "g2", "g3", "g4"),
    protected_approval_event_id: str | None = None,
) -> RunManifest:
    proposal_id = f"proposal-{proposal_hex}"
    return RunManifest(
        run_id=f"run-{run_hex}",
        repository=repository,
        base_sha=base_sha,
        worktree_path=tmp_path / "worktrees" / proposal_id,
        proposal_id=proposal_id,
        attempt=1,
        run_kind="implementation",
        branch=f"selfdev/{proposal_id}",
        proposal_manifest_ref="proposal.json",
        proposal_manifest_sha256=proposal_manifest_sha256,
        scope=scope,
        scope_sha256=scope_sha256(scope),
        acceptance_criteria=("Wave 2 test",),
        required_gates=required_gates,
        writer_runtime={"role": "S1_WORKER", "backend": "fake", "model": "test"},
        budget={"tokens": 100, "wall_clock_seconds": 30},
        risk_class=risk_class,
        initial_decision_event_id="decision-wave2",
        protected_approval_event_id=protected_approval_event_id,
    )


@pytest.fixture
def _mock_external_gate_commands(monkeypatch: pytest.MonkeyPatch):
    """G2/G3/G4 の外部 subprocess を固定し、Git のみ実行させる。"""

    calls: list[tuple[tuple[str, ...], Path, bool]] = []

    def fake_run_command(
        command: tuple[str, ...] | list[str],
        *,
        cwd: Path,
        log_path: Path,
        trusted: bool = False,
    ) -> int:
        del log_path
        calls.append((tuple(command), cwd, trusted))
        return 0

    monkeypatch.setattr(runner, "_run_command", fake_run_command)
    return calls


def _run_v2(
    repository: Path,
    base_sha: str,
    manifest: RunManifest,
    output_root: Path,
    *,
    protected_scope_sha256: str | None = None,
    protected_approval: dict[str, str] | None = None,
) -> tuple[dict[str, object], int]:
    return runner.run(
        repository,
        base=base_sha,
        # Deliberately exercise case/space normalization at the runner boundary.
        gates=" G1, g2, G3, g4 ",
        out=output_root,
        proposal_id=manifest.proposal_id,
        implementation_run_id=manifest.run_id,
        gate_attempt=manifest.attempt,
        scope=list(manifest.scope),
        scope_sha256=manifest.scope_sha256,
        risk_class=manifest.risk_class,
        proposal_manifest_sha256=manifest.proposal_manifest_sha256,
        protected_scope_sha256=protected_scope_sha256,
        protected_approval=protected_approval,
    )


def test_proposal_workspace_snapshots_audit_and_cleans_only_on_terminal(
    tmp_path: Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
    )
    workspace = ProposalWorkspace(manifest=manifest, run_dir=tmp_path / "runs" / "implementation")

    assert workspace.create() == manifest.worktree_path
    descriptor_before_lifecycle = workspace.descriptor_path.read_bytes()
    (manifest.worktree_path / "src" / "module.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    (manifest.worktree_path / "src" / "new_module.py").write_text(
        "VALUE = 3\n", encoding="utf-8"
    )

    patch_path = workspace.snapshot()
    artifact_dir = workspace.run_dir / "artifacts"
    audit = json.loads((artifact_dir / "workspace-audit.json").read_text(encoding="utf-8"))

    assert workspace.status is WorkspaceStatus.SNAPSHOTTED
    assert workspace.descriptor_path.read_bytes() == descriptor_before_lifecycle
    assert manifest.worktree_path.exists()
    assert patch_path == artifact_dir / "candidate.patch"
    assert "VALUE = 2" in patch_path.read_text(encoding="utf-8")
    assert "new_module.py" in patch_path.read_text(encoding="utf-8")
    assert (artifact_dir / "git-status.txt").read_text(encoding="utf-8").strip()
    assert (artifact_dir / "git-diff-summary.txt").exists()
    assert audit["proposal_id"] == manifest.proposal_id
    assert audit["changed_paths"] == ["src/module.py", "src/new_module.py"]
    assert audit["candidate_diff_sha256"] == candidate_diff_sha256(
        manifest.worktree_path, base_sha
    )

    workspace.finalize(terminal=True)

    assert workspace.status is WorkspaceStatus.CLOSED
    assert workspace.descriptor_path.read_bytes() == descriptor_before_lifecycle
    assert json.loads((workspace.run_dir / "workspace-state.json").read_text(encoding="utf-8"))["status"] == "closed"
    assert not manifest.worktree_path.exists()
    assert all(item.path != manifest.worktree_path for item in list_worktrees(repository))


def test_proposal_workspace_cleanup_ignores_pytest_nested_repository(tmp_path: Path) -> None:
    """無関係な pytest 残骸を workspace の変更走査へ混入させない。"""

    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
    )
    workspace = ProposalWorkspace(manifest=manifest, run_dir=tmp_path / "runs" / "cleanup")
    workspace.create()

    (manifest.worktree_path / ".gitignore").write_text(".pytest-tmp/\n", encoding="utf-8")
    nested_repository = (
        manifest.worktree_path / ".pytest-tmp" / "pid-78" / "test_candidate_committer_create" / "repository"
    )
    nested_repository.mkdir(parents=True)
    _git(nested_repository, "init", "-b", "main")
    (nested_repository / "unrelated.txt").write_text("not a candidate\n", encoding="utf-8")

    workspace.finalize(terminal=True)

    assert not manifest.worktree_path.exists()
    assert workspace.skipped_paths == ()
    audit = json.loads(
        (workspace.run_dir / "artifacts" / "workspace-audit.json").read_text(encoding="utf-8")
    )
    assert not any(path.startswith(".pytest-tmp/") for path in audit["changed_paths"])


def test_collect_changed_paths_skips_invalid_git_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    skipped: list[str] = []

    def fake_git_output(_cwd: Path, *args: str, **_kwargs: object) -> str:
        if args[0] == "diff":
            return "bad/\n"
        if args[0] == "ls-files":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(selfdev_git, "git_output", fake_git_output)

    assert selfdev_git.collect_changed_paths(tmp_path, "base", skipped_paths=skipped) == ()
    assert skipped == ["bad/"]


def test_proposal_workspace_rejects_initial_path_and_branch_collisions(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
    )
    manifest.worktree_path.parent.mkdir(parents=True)

    _git(
        repository,
        "worktree",
        "add",
        "-b",
        "occupied",
        str(manifest.worktree_path),
        base_sha,
    )
    workspace = ProposalWorkspace(manifest=manifest, run_dir=tmp_path / "runs" / "collision-path")
    with pytest.raises(WorkspaceError, match="path または branch が既に存在"):
        workspace.create()
    _git(repository, "worktree", "remove", "--force", str(manifest.worktree_path))

    _git(repository, "branch", manifest.branch, base_sha)
    workspace = ProposalWorkspace(manifest=manifest, run_dir=tmp_path / "runs" / "collision-branch")
    with pytest.raises(WorkspaceError, match="path または branch が既に存在"):
        workspace.create()


@pytest.mark.asyncio
async def test_platform_shutdown_keeps_proposal_worktree_registered(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
    )
    config = RunConfig(
        agents=AgentsConfig(
            default_backend="fake",
            roles={role: "fake" for role in SystemRole},
        ),
        selfdev=SelfDevConfig(enabled=True, repository=repository),
    )
    runtimes = {role: FakeAgentRuntime() for role in SystemRole}
    platform = await Platform.create(
        run_id=manifest.run_id,
        runs_dir=tmp_path / "runs",
        run_config=config,
        manifest=manifest,
        runtime_overrides=runtimes,
    )
    controller = platform.workspace_controller
    assert controller is not None
    try:
        await platform.shutdown()
        assert manifest.worktree_path.exists()
        assert any(item.path == manifest.worktree_path for item in list_worktrees(repository))
    finally:
        if manifest.worktree_path.exists():
            controller.discard()


def test_proposal_run_manifest_normalises_required_gates(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
        required_gates=(" G1 ", "G2", " g3", "g4 "),
    )

    assert manifest.required_gates == REQUIRED_GATES

    with pytest.raises(ValueError, match="required_gates"):
        _manifest(
            tmp_path,
            repository,
            base_sha,
            scope=({"path": "src", "kind": "tree"},),
            required_gates=("g1", "g2", "g3", "unknown"),
        )


def test_gate_runner_v2_allows_scoped_tracked_and_untracked_changes(
    tmp_path: Path,
    _mock_external_gate_commands,
) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
    )
    (repository / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repository / "src" / "new_module.py").write_text("VALUE = 3\n", encoding="utf-8")

    report, exit_code = _run_v2(repository, base_sha, manifest, tmp_path / "reports")

    assert exit_code == 0
    assert report["status"] == "pass"
    assert report["gates_requested"] == list(REQUIRED_GATES)
    assert report["scope_violations"] == []
    assert set(report["changed_paths"]) == {"src/module.py", "src/new_module.py"}
    assert report["gates"]["g1"]["status"] == "pass"
    assert (tmp_path / "reports" / "gate_report.json").exists()
    assert not (repository / "reports" / "gate_report.json").exists()


def test_gate_runner_v2_fails_for_scope_outside_tracked_change(
    tmp_path: Path,
    _mock_external_gate_commands,
) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
    )
    (repository / "outside.txt").write_text("outside changed\n", encoding="utf-8")

    report, exit_code = _run_v2(repository, base_sha, manifest, tmp_path / "reports")

    assert exit_code == 1
    assert report["status"] == "fail"
    assert report["scope_violations"] == ["outside.txt"]
    assert report["gates"]["g1"]["status"] == "fail"
    assert "scope outside" in report["gates"]["g1"]["summary"]


def test_gate_runner_v2_mocks_g2_and_g3_subprocesses(
    tmp_path: Path,
    _mock_external_gate_commands,
) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "frontend", "kind": "tree"},),
    )
    (repository / "frontend" / "App.tsx").write_text("export {}\n", encoding="utf-8")

    report, exit_code = _run_v2(repository, base_sha, manifest, tmp_path / "reports")
    calls = _mock_external_gate_commands
    commands = [command for command, _, trusted in calls]

    assert exit_code == 0
    assert report["gates"]["g3"]["status"] == "pass"
    assert tuple(runner.G2_COMMAND) in commands
    assert tuple(runner.G3_LINT_COMMAND) in commands
    assert tuple(runner.G3_BUILD_COMMAND) in commands
    assert all(trusted for _, _, trusted in calls)


@pytest.mark.parametrize(
    ("approval_kind", "expected_status"),
    (("missing", "fail"), ("mismatch", "fail"), ("matching", "pass")),
)
def test_gate_runner_v2_protected_path_requires_matching_hash_approval(
    tmp_path: Path,
    _mock_external_gate_commands,
    approval_kind: str,
    expected_status: str,
) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "vsm/gates", "kind": "tree"},),
        risk_class="protected",
        protected_approval_event_id="human-approval-1",
    )
    protected_scope_hash = "d" * 64
    approval = None
    if approval_kind != "missing":
        approval = {
            "event_id": "human-approval-1",
            "proposal_manifest_sha256": manifest.proposal_manifest_sha256,
            "protected_scope_sha256": (
                protected_scope_hash if approval_kind == "matching" else "e" * 64
            ),
        }
    (repository / "vsm" / "gates" / "policy.py").write_text(
        "POLICY = 'changed'\n", encoding="utf-8"
    )

    report, exit_code = _run_v2(
        repository,
        base_sha,
        manifest,
        tmp_path / f"reports-{approval_kind}",
        protected_scope_sha256=protected_scope_hash,
        protected_approval=approval,
    )

    assert report["status"] == expected_status
    assert exit_code == (0 if expected_status == "pass" else 1)
    if expected_status == "pass":
        assert report["protected_paths"] == []
        assert report["protected_approval_event_id"] == "human-approval-1"
    else:
        assert report["protected_paths"] == ["vsm/gates/policy.py"]
        assert report["gates"]["g1"]["status"] == "fail"


@pytest.mark.asyncio
async def test_gate_report_v2_missing_metadata_fails_fast() -> None:
    appended: list[tuple[str, dict[str, object]]] = []

    class EventLog:
        async def append(self, event_type: str, payload: dict[str, object], **kwargs) -> None:
            del kwargs
            appended.append((event_type, payload))

    report = {
        "schema_version": 2,
        "status": "pass",
        "gates": {"g1": {"status": "pass"}},
    }
    with pytest.raises(ValueError, match="GateReport v2 の event metadata が不足"):
        await record_gate_report_generated(EventLog(), report)

    assert appended == []


def test_candidate_committer_creates_proposal_bound_commit_after_gate_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_external_gate_commands,
) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
    )
    workspace = ProposalWorkspace(manifest=manifest, run_dir=tmp_path / "runs" / "candidate")
    worktree = workspace.acquire()
    (worktree / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (worktree / "src" / "new_module.py").write_text("VALUE = 3\n", encoding="utf-8")
    report, exit_code = _run_v2(worktree, base_sha, manifest, tmp_path / "gates")
    assert exit_code == 0

    commit_operations: list[tuple[str, ...]] = []
    original_run_commit = selfdev_git._run_commit

    def tracked_run_commit(candidate: Path, args: list[str]) -> None:
        del candidate
        commit_operations.append(tuple(args))
        original_run_commit(worktree, args)

    monkeypatch.setattr(selfdev_git, "_run_commit", tracked_run_commit)
    try:
        candidate = workspace.commit_candidate(gate_report=report)

        assert candidate.proposal_id == manifest.proposal_id
        assert candidate.parent_sha == base_sha
        assert candidate.base_sha == manifest.base_sha
        assert candidate.branch == manifest.branch
        assert candidate.diff_sha256 == report["candidate_diff_sha256"]
        assert _git(worktree, "rev-parse", "HEAD") == candidate.commit_sha + "\n"
        assert _git(worktree, "rev-parse", "HEAD^{tree}").strip() == candidate.tree_sha
        assert _git(worktree, "rev-parse", "HEAD^").strip() == base_sha

        message = _git(worktree, "show", "-s", "--format=%B", candidate.commit_sha)
        assert f"Proposal-ID: {manifest.proposal_id}" in message
        assert f"Base-SHA: {base_sha}" in message
        assert f"Candidate-Diff-SHA256: {candidate.diff_sha256}" in message
        changed = set(
            _git(worktree, "diff", "--name-only", base_sha, candidate.commit_sha, "--")
            .splitlines()
        )
        assert changed == {"src/module.py", "src/new_module.py"}
        assert _git(worktree, "status", "--short") == ""
        assert commit_operations[0] == ("add", "-A", "--")
        assert commit_operations[1][0] == "commit"
        assert not any(operation[0] in {"push", "merge"} for operation in commit_operations)
    finally:
        if worktree.exists():
            workspace.finalize(terminal=True)


def test_candidate_committer_rejects_gate_report_without_required_metadata(
    tmp_path: Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
    )
    workspace = ProposalWorkspace(manifest=manifest, run_dir=tmp_path / "runs" / "missing-metadata")
    worktree = workspace.acquire()
    (worktree / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    report: dict[str, object] = {
        "schema_version": 2,
        "status": "pass",
        "proposal_id": manifest.proposal_id,
        "implementation_run_id": manifest.run_id,
        "gate_attempt": manifest.attempt,
        "base_sha": manifest.base_sha,
        "scope_sha256": manifest.scope_sha256,
        "gates": {name: {"status": "pass"} for name in REQUIRED_GATES},
    }
    try:
        with pytest.raises(CandidateCommitError, match="candidate diff digest"):
            workspace.commit_candidate(gate_report=report)
        assert _git(worktree, "rev-parse", "HEAD").strip() == base_sha
    finally:
        if worktree.exists():
            workspace.finalize(terminal=True)


@pytest.mark.asyncio
async def test_generated_gate_report_v2_event_payload_is_strict(
    tmp_path: Path,
    _mock_external_gate_commands,
) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(
        tmp_path,
        repository,
        base_sha,
        scope=({"path": "src", "kind": "tree"},),
    )
    (repository / "src" / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    report, _ = _run_v2(repository, base_sha, manifest, tmp_path / "reports")
    appended: list[dict[str, object]] = []

    class EventLog:
        async def append(self, event_type: str, payload: dict[str, object], **kwargs) -> None:
            assert event_type == "gate_report_generated"
            assert kwargs["schema_version"] == 2
            appended.append(payload)

    await record_gate_report_generated(EventLog(), report)

    assert len(appended) == 1
    validate_event_payload("gate_report_generated", 2, appended[0])
