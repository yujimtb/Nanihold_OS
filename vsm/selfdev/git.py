"""自己開発 controller が使う、候補 worktree 専用の Git 操作。

このモジュールは候補 worktree から import されない control-plane のコードで
ある。agent に渡す API はなく、候補 commit は controller 側の
``CandidateCommitter`` だけが作成する。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from vsm.errors import CandidateCommitError, WorkspaceError

__all__ = [
    "CandidateCommit",
    "CandidateCommitter",
    "GitWorktreeInfo",
    "candidate_diff_sha256",
    "collect_changed_paths",
    "git_output",
    "list_worktrees",
]


def _git_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("GIT_"):
            env.pop(key, None)
    return env


def git_output(cwd: Path, *args: str, check: bool = True) -> str:
    """Git の stdout を返す。失敗は握りつぶさず typed error にする。"""

    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=_git_environment(),
        )
    except OSError as exc:
        raise WorkspaceError(f"git を実行できません: {' '.join(args)}: {exc}") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise WorkspaceError(
            f"git {' '.join(args)} に失敗しました (exit={completed.returncode}): {detail}"
        )
    return completed.stdout


@dataclass(frozen=True, slots=True)
class GitWorktreeInfo:
    path: Path
    head: str
    branch: str | None


def list_worktrees(repository: Path) -> tuple[GitWorktreeInfo, ...]:
    output = git_output(repository, "worktree", "list", "--porcelain")
    entries: list[GitWorktreeInfo] = []
    current: dict[str, str] = {}
    for line in (*output.splitlines(), ""):
        if line:
            key, _, value = line.partition(" ")
            if key:
                current[key] = value
            continue
        if current.get("worktree") and current.get("HEAD"):
            entries.append(
                GitWorktreeInfo(
                    path=Path(current["worktree"]).resolve(strict=False),
                    head=current["HEAD"],
                    branch=current.get("branch"),
                )
            )
        current = {}
    return tuple(entries)


def _relative(path: str) -> str:
    value = path.replace("\\", "/")
    if not value or value.startswith("/") or "\x00" in value:
        raise WorkspaceError(f"Git path が不正です: {path!r}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise WorkspaceError(f"Git path が不正です: {path!r}")
    return value


def collect_changed_paths(
    worktree: Path,
    base_sha: str,
    *,
    skipped_paths: list[str] | None = None,
) -> tuple[str, ...]:
    """変更 path を返す。

    Git の ``--others`` は、``.gitignore`` による除外を明示しないと
    selfdev worktree 内に残った pytest の一時 repository まで列挙する。
    control plane が管理する変更だけを対象にし、無関係または Git path
    として正規化できない値はスキップする。
    """

    tracked = git_output(
        worktree, "diff", "--no-ext-diff", "--name-only", "--no-renames", base_sha, "--"
    )
    untracked = git_output(
        worktree,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
    )
    paths: set[str] = set()
    for value in (*tracked.splitlines(), *untracked.split("\0")):
        if not value:
            continue
        try:
            paths.add(_relative(value))
        except WorkspaceError:
            if skipped_paths is not None:
                skipped_paths.append(value)
    return tuple(sorted(paths))


def candidate_diff_sha256(
    worktree: Path,
    base_sha: str,
    *,
    skipped_paths: list[str] | None = None,
) -> str:
    """作業木の内容から計算する、stage/commit を跨いで安定した digest。"""

    rows: list[dict[str, Any]] = []
    for relative in collect_changed_paths(worktree, base_sha, skipped_paths=skipped_paths):
        path = worktree.joinpath(*relative.split("/"))
        if path.is_symlink():
            rows.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
        elif path.exists() and path.is_file():
            rows.append({"path": relative, "kind": "file", "content": path.read_bytes().hex()})
        elif not path.exists():
            rows.append({"path": relative, "kind": "deleted"})
        else:
            raise CandidateCommitError(f"変更 path が通常ファイルではありません: {relative}")
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _run_commit(worktree: Path, args: list[str]) -> None:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=_git_environment(),
        )
    except OSError as exc:
        raise CandidateCommitError(f"candidate commit の git 実行に失敗しました: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise CandidateCommitError(
            f"candidate commit に失敗しました (exit={completed.returncode}): {detail}"
        )


@dataclass(frozen=True, slots=True)
class CandidateCommit:
    proposal_id: str
    commit_sha: str
    parent_sha: str
    tree_sha: str
    branch: str
    base_sha: str
    diff_sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "proposal_id": self.proposal_id,
            "commit_sha": self.commit_sha,
            "parent_sha": self.parent_sha,
            "tree_sha": self.tree_sha,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "diff_sha256": self.diff_sha256,
        }


class CandidateCommitter:
    """Gate通過後の候補 commit を一度だけ作成する controller 境界。"""

    def __init__(self, *, manifest: Any | None = None, worktree: Path | None = None) -> None:
        self.manifest = manifest
        self.worktree = worktree.resolve(strict=False) if worktree is not None else None

    def commit_candidate(
        self,
        *,
        manifest: Any | None = None,
        worktree: Path | None = None,
        gate_report: Mapping[str, Any] | Any,
    ) -> CandidateCommit:
        current_manifest = manifest or self.manifest
        candidate = (worktree or self.worktree)
        if current_manifest is None or candidate is None:
            raise CandidateCommitError("manifest と worktree は必須です")
        if getattr(current_manifest, "proposal_id", None) is None:
            raise CandidateCommitError("候補 commit は Proposal RunManifest にだけ許可されます")
        candidate = candidate.resolve(strict=False)
        proposal_id = str(current_manifest.proposal_id)
        base_sha = str(current_manifest.base_sha)
        branch = str(current_manifest.branch)

        report = gate_report.model_dump(mode="json") if hasattr(gate_report, "model_dump") else dict(gate_report)
        if report.get("schema_version") != 2 or report.get("status") != "pass":
            raise CandidateCommitError("pass の GateReport v2 が必要です")
        if report.get("proposal_id") != proposal_id or report.get("implementation_run_id") != current_manifest.run_id:
            raise CandidateCommitError("GateReport の Proposal/Run が manifest と不一致です")
        if report.get("base_sha") != base_sha or report.get("gate_attempt") != current_manifest.attempt:
            raise CandidateCommitError("GateReport の base/attempt が manifest と不一致です")
        if report.get("scope_sha256") != current_manifest.scope_sha256:
            raise CandidateCommitError("GateReport の scope hash が manifest と不一致です")
        gates = report.get("gates")
        if not isinstance(gates, Mapping) or set(gates) != {"g1", "g2", "g3", "g4"}:
            raise CandidateCommitError("GateReport は g1〜g4 全件を必要とします")
        if any(detail.get("status") not in {"pass", "skip"} for detail in gates.values()):
            raise CandidateCommitError("error/fail の gate report から commit できません")

        actual_branch = git_output(candidate, "branch", "--show-current").strip()
        if actual_branch != branch:
            raise CandidateCommitError(f"candidate branch が不一致です: {actual_branch!r} != {branch!r}")
        parent_sha = git_output(candidate, "rev-parse", "HEAD").strip()
        if parent_sha != base_sha:
            raise CandidateCommitError("candidate commit 前の HEAD が base_sha と一致しません")
        actual_digest = candidate_diff_sha256(candidate, base_sha)
        if actual_digest != report.get("candidate_diff_sha256"):
            raise CandidateCommitError("gate 後に candidate diff digest が変化しました")
        if not collect_changed_paths(candidate, base_sha):
            raise CandidateCommitError("変更のない candidate commit は作成できません")

        _run_commit(candidate, ["add", "-A", "--"])
        message = f"selfdev: candidate {proposal_id}"
        _run_commit(
            candidate,
            [
                "commit",
                "--no-verify",
                "-m",
                message,
                "--trailer",
                f"Proposal-ID: {proposal_id}",
                "--trailer",
                f"Base-SHA: {base_sha}",
                "--trailer",
                f"Candidate-Diff-SHA256: {actual_digest}",
            ],
        )
        commit_sha = git_output(candidate, "rev-parse", "HEAD").strip()
        tree_sha = git_output(candidate, "rev-parse", "HEAD^{tree}").strip()
        return CandidateCommit(
            proposal_id=proposal_id,
            commit_sha=commit_sha,
            parent_sha=parent_sha,
            tree_sha=tree_sha,
            branch=branch,
            base_sha=base_sha,
            diff_sha256=actual_digest,
        )
