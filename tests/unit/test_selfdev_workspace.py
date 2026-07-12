"""self-hosting workspace の決定論テスト。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vsm.agents.backends.fake import FakeAgentRuntime
from vsm.config import AgentsConfig, RunConfig, SelfDevConfig, load_config
from vsm.errors import WorkspaceError, WorkspacePolicyError
from vsm.roles import SystemRole
from vsm.runtime.lifecycle import Platform
from vsm.runtime.manifest import RunManifest, WorkspaceController, find_orphan_worktrees


@pytest.fixture(autouse=True)
def _allow_test_repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose の root 実行でも一時 repository を Git に認識させる。"""

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Nanihold Test")
    source = repository / "src"
    source.mkdir()
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "src/module.py")
    _git(repository, "commit", "-m", "initial")
    base_sha = _git(repository, "rev-parse", "HEAD").strip()
    return repository, base_sha


def _manifest(tmp_path: Path, repository: Path, base_sha: str, run_id: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        repository=repository,
        base_sha=base_sha,
        worktree_path=tmp_path / "worktrees" / run_id,
        backend="codex",
        model="test/model",
        budget={"tokens": 1000, "wall_clock_seconds": 60},
        risk_class="repo_write",
        issued_by={"decision": "decision-test", "conversation": "conversation-test"},
        allowed_paths=("src/",),
        acceptance_criteria=("module.py が変更される",),
        required_gates=("pytest",),
    )


def test_manifest_worktree_patch_and_cleanup(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    run_dir = tmp_path / "runs" / "run-selfdev"
    manifest = _manifest(tmp_path, repository, base_sha, "run-selfdev")

    manifest_path = manifest.persist(run_dir)
    loaded = RunManifest.load(run_dir)
    assert manifest_path == run_dir / "manifest.json"
    assert loaded == manifest

    controller = WorkspaceController(manifest=loaded, run_dir=run_dir)
    controller.start()
    assert (manifest.worktree_path / "src/module.py").exists()
    (manifest.worktree_path / "src/module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (manifest.worktree_path / "src/new_module.py").write_text(
        "VALUE = 4\n", encoding="utf-8"
    )

    candidate = controller.interrupt()
    assert candidate.read_text(encoding="utf-8")
    assert "VALUE = 2" in candidate.read_text(encoding="utf-8")
    assert "new_module.py" in candidate.read_text(encoding="utf-8")
    assert (run_dir / "git-status.txt").exists()
    assert (run_dir / "git-diff-summary.txt").exists()
    assert (run_dir / "workspace-audit.json").exists()
    assert not manifest.worktree_path.exists()
    assert find_orphan_worktrees(repository, [manifest]) == []


def test_orphan_worktree_detection_compares_manifest_paths(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(tmp_path, repository, base_sha, "run-orphan")
    controller = WorkspaceController(manifest=manifest, run_dir=tmp_path / "run")
    controller.start()

    assert find_orphan_worktrees(repository, [manifest]) == []
    assert find_orphan_worktrees(repository, []) == [manifest.worktree_path]
    controller.discard()


def test_forbidden_change_is_saved_before_policy_error(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(tmp_path, repository, base_sha, "run-protected")
    manifest = RunManifest(
        **{
            **manifest.to_dict(),
            "repository": repository,
            "worktree_path": manifest.worktree_path,
            "forbidden_paths": ("src/",),
        }
    )
    controller = WorkspaceController(manifest=manifest, run_dir=tmp_path / "run")
    controller.start()
    (manifest.worktree_path / "src/module.py").write_text("VALUE = 3\n", encoding="utf-8")

    with pytest.raises(WorkspacePolicyError):
        controller.finalize()
    assert (tmp_path / "run/candidate.patch").exists()
    assert not manifest.worktree_path.exists()


@pytest.mark.asyncio
async def test_platform_propagates_manifest_worktree_to_fake_runtime(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    run_id = "run-propagation"
    manifest = _manifest(tmp_path, repository, base_sha, run_id)
    fake_runtimes = {
        role: FakeAgentRuntime()
        for role in SystemRole
    }
    config = RunConfig(
        agents=AgentsConfig(
            default_backend="fake",
            roles={role: "fake" for role in SystemRole},
        ),
        selfdev=SelfDevConfig(enabled=True, repository=repository),
    )
    platform = await Platform.create(
        run_id=run_id,
        runs_dir=tmp_path / "runs",
        run_config=config,
        manifest=manifest,
        runtime_overrides=fake_runtimes,
    )
    try:
        s5 = platform.systems[SystemRole.S5_POLICY][0]
        await s5.sub_agents[0].respond("inspect")
        assert fake_runtimes[SystemRole.S5_POLICY].invocations[-1].workdir == manifest.worktree_path

        s1 = await platform.spawn_s1(specialization="test", initial_assignment="inspect")
        await s1.sub_agents[0].respond("modify")
        assert fake_runtimes[SystemRole.S1_WORKER].invocations[-1].workdir == manifest.worktree_path
    finally:
        await platform.shutdown()


def test_one_run_one_writer_guard(tmp_path: Path) -> None:
    repository, base_sha = _repository(tmp_path)
    manifest = _manifest(tmp_path, repository, base_sha, "run-writer")
    controller = WorkspaceController(manifest=manifest, run_dir=tmp_path / "run")
    controller.start()
    platform = SimpleNamespace(
        workspace_controller=controller,
        workdir=manifest.worktree_path,
        _active_s1_writer=None,
    )
    # Platform の実メソッドを副作用なしに検査するため、束縛して使う。
    reserve = Platform.reserve_s1_writer.__get__(platform, Platform)
    first = SimpleNamespace(system_id="s1-a", _runtime=FakeAgentRuntime())
    second = SimpleNamespace(system_id="s1-b", _runtime=FakeAgentRuntime())
    reserve(first)
    with pytest.raises(WorkspaceError, match="1 Run 1 writer"):
        reserve(second)
    controller.discard()


def test_selfdev_toml_defaults_and_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "vsm.toml"
    config_path.write_text(
        """
[selfdev]
enabled = true
repository = "repo"
forbidden_paths = ["AGENTS.md", "protected/"]
""",
        encoding="utf-8",
    )
    _, run_config = load_config(config_path)
    assert run_config.selfdev.enabled is True
    assert run_config.selfdev.repository == (tmp_path / "repo").resolve()
    assert run_config.selfdev.forbidden_paths == ("AGENTS.md", "protected/")
