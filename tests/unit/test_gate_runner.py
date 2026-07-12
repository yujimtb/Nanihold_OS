"""Trusted GateRunner の決定論的なポリシー・レポート・実行契約テスト。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from vsm.eventlog.schema import validate_event_payload
from vsm.gates.events import record_gate_report_generated
from vsm.gates import runner


def _git(repo: Path, *args: str) -> None:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("GIT_"):
            environment.pop(key, None)
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr


def _repo(tmp_path: Path) -> Path:
    # The repository must live outside the checked-out worktree.  The Docker
    # test mount uses a Windows worktree .git pointer, which Git cannot use for
    # nested repositories under /workspace; /tmp is still a real tmp git repo.
    repo = Path(tempfile.mkdtemp(prefix="gate-runner-repo-"))
    _git(repo, "init")
    _git(repo, "config", "user.email", "gate@example.invalid")
    _git(repo, "config", "user.name", "Gate Test")
    (repo / "src.py").write_text("value = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".env.*\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo


def _run_g1(repo: Path, out: Path) -> tuple[dict[str, object], int]:
    return runner.run(repo, base="HEAD", gates="g1", out=out)


@pytest.mark.parametrize(
    "relative_path",
    [
        "AGENTS.md",
        ".github/workflows/policy.yml",
        "vsm/gates/policy.py",
        "openspec/changes/change/specs/example/spec.md",
        "vsm.toml",
    ],
)
def test_g1_rejects_protected_paths(tmp_path: Path, relative_path: str) -> None:
    repo = _repo(tmp_path)
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n", encoding="utf-8")

    report, exit_code = _run_g1(repo, tmp_path / "out")

    assert exit_code == 1
    assert report["gates"]["g1"]["status"] == "fail"  # type: ignore[index]
    assert relative_path in report["gates"]["g1"]["summary"]  # type: ignore[index]


def test_g1_rejects_secret_env_and_untracked_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "config.py").write_text(
        'api_key = "0123456789abcdef"\n', encoding="utf-8"
    )
    (repo / ".env.local").write_text("TOKEN_KEY=0123456789abcdef\n", encoding="utf-8")

    report, exit_code = _run_g1(repo, tmp_path / "out")
    summary = report["gates"]["g1"]["summary"]  # type: ignore[index]

    assert exit_code == 1
    assert "secret-like content" in summary
    assert "env-file" in summary
    assert "untracked files" in summary


def test_g1_rejects_symlink_large_diff_and_diff_check(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src.py").write_text("value = 1  \n", encoding="utf-8")
    (repo / "link").symlink_to("src.py")
    (repo / "large.txt").write_text("x\n" * 8001, encoding="utf-8")

    report, exit_code = _run_g1(repo, tmp_path / "out")
    summary = report["gates"]["g1"]["summary"]  # type: ignore[index]

    assert exit_code == 1
    assert "symbolic link changed" in summary
    assert "diff too large" in summary
    assert "git diff --check failed" in summary


def test_report_contains_each_gate_status_and_log_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report, exit_code = runner.run(repo, base="HEAD", gates="g1,g3", out=tmp_path / "out")

    assert exit_code == 0
    assert report["status"] == "pass"
    gates = report["gates"]
    assert gates["g1"]["status"] == "pass"
    assert gates["g3"]["status"] == "skip"
    assert Path(gates["g1"]["log_path"]).exists()
    assert json.loads((tmp_path / "out" / "gate_report.json").read_text(encoding="utf-8")) == report


def test_g2_is_fixed_subprocess_and_failure_returns_one(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 1, "pytest output", "failed")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    report, exit_code = runner.run(repo, gates="g2", out=tmp_path / "out")

    assert exit_code == 1
    assert report["gates"]["g2"]["status"] == "fail"
    assert tuple(runner.G2_COMMAND) in calls


def test_g4_uses_help_and_fake_smoke_subprocess(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_run(command, **kwargs):
        calls.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    report, exit_code = runner.run(repo, gates="g4", out=tmp_path / "out")

    assert exit_code == 0
    assert report["gates"]["g4"]["status"] == "pass"
    assert tuple(runner.G4_HELP_COMMAND) in calls
    assert tuple(runner.G4_SMOKE_COMMAND) in calls


def test_execution_unavailable_returns_two_and_is_reported(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(runner.subprocess, "run", unavailable)
    report, exit_code = runner.run(repo, gates="g2", out=tmp_path / "out")

    assert exit_code == 2
    assert report["status"] == "execution_error"
    assert report["gates"]["g2"]["status"] == "skip"


def test_g3_runs_only_for_frontend_changes(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    package = repo / "frontend" / "package.json"
    package.parent.mkdir()
    package.write_text(
        json.dumps({"scripts": {"lint": "eslint .", "build": "vite build"}}),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "frontend base")
    (repo / "frontend" / "App.tsx").write_text("export {}\n", encoding="utf-8")

    commands: list[tuple[str, ...]] = []

    def fake_command(command, *, cwd, log_path):
        commands.append(tuple(command))
        return 0

    monkeypatch.setattr(runner, "_run_command", fake_command)
    report, exit_code = runner.run(repo, gates="g3", out=tmp_path / "out")

    assert exit_code == 0
    assert report["gates"]["g3"]["status"] == "pass"
    assert tuple(runner.G3_LINT_COMMAND) in commands
    assert tuple(runner.G3_BUILD_COMMAND) in commands


@pytest.mark.asyncio
async def test_gate_report_event_schema_and_recording() -> None:
    payloads: list[tuple[str, dict[str, object]]] = []

    class EventLog:
        async def append(self, event_type: str, payload: dict[str, object], **kwargs) -> None:
            payloads.append((event_type, payload))

    report = {
        "report_path": "/tmp/gate_report.json",
        "worktree": "/tmp/worktree",
        "base": "HEAD",
        "status": "pass",
        "gates": {"g1": {"status": "pass"}, "g3": {"status": "skip"}},
    }
    await record_gate_report_generated(EventLog(), report)  # type: ignore[arg-type]

    event_type, payload = payloads[0]
    assert event_type == "gate_report_generated"
    validate_event_payload(event_type, payload)
    assert payload["gate_statuses"] == {"g1": "pass", "g3": "skip"}
