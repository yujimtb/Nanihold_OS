"""Proposal が所有する self-development workspace の lifecycle。"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from vsm.errors import WorkspaceError
from vsm.runtime.manifest import RunManifest
from vsm.selfdev.git import (
    CandidateCommit,
    CandidateCommitter,
    GitWorktreeInfo,
    candidate_diff_sha256,
    collect_changed_paths,
    git_output,
    list_worktrees,
)
from vsm.selfdev.state_machine import ProposalPhase, TERMINAL_PHASES

__all__ = ["WorkspaceDescriptor", "WorkspaceStatus", "ProposalWorkspace", "WorkspaceController"]


class WorkspaceStatus(StrEnum):
    READY = "ready"
    IN_USE = "in_use"
    SNAPSHOTTED = "snapshotted"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class WorkspaceDescriptor:
    schema_version: int
    proposal_id: str
    repository: Path
    base_sha: str
    branch: str
    worktree_path: Path
    status: WorkspaceStatus

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise WorkspaceError("workspace descriptor の schema_version は1固定です")
        if isinstance(self.status, str):
            object.__setattr__(self, "status", WorkspaceStatus(self.status))
        if not self.proposal_id or not self.base_sha or not self.branch:
            raise WorkspaceError("workspace descriptor の識別情報は必須です")

    @classmethod
    def from_manifest(cls, manifest: RunManifest, *, status: WorkspaceStatus) -> "WorkspaceDescriptor":
        if manifest.proposal_id is None:
            raise WorkspaceError("Proposal workspace には proposal_id が必要です")
        return cls(
            schema_version=1,
            proposal_id=manifest.proposal_id,
            repository=manifest.repository,
            base_sha=manifest.base_sha,
            branch=manifest.branch,
            worktree_path=manifest.worktree_path,
            status=status,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "repository": str(self.repository),
            "base_sha": self.base_sha,
            "branch": self.branch,
            "worktree_path": str(self.worktree_path),
            "status": self.status.value,
        }

    @classmethod
    def load(cls, path: Path) -> "WorkspaceDescriptor":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(f"workspace descriptor を読み込めません: {path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise WorkspaceError("workspace descriptor の schema_version は1固定です")
        try:
            return cls(
                schema_version=1,
                proposal_id=str(payload["proposal_id"]),
                repository=Path(payload["repository"]).resolve(strict=False),
                base_sha=str(payload["base_sha"]),
                branch=str(payload["branch"]),
                worktree_path=Path(payload["worktree_path"]).resolve(strict=False),
                status=WorkspaceStatus(str(payload["status"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkspaceError(f"workspace descriptor が不正です: {path}: {exc}") from exc


def _write_descriptor(path: Path, descriptor: WorkspaceDescriptor) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    data = (json.dumps(descriptor.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _untracked_patch(worktree: Path, relative: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", relative],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise WorkspaceError(f"untracked patch を取得できません: {relative}: {exc}") from exc
    if completed.returncode not in (0, 1):
        raise WorkspaceError(f"untracked patch を取得できません: {relative}: {completed.stderr.strip()}")
    return completed.stdout


def _worktree_patch(worktree: Path, base_sha: str) -> tuple[str, str, tuple[str, ...]]:
    status = git_output(worktree, "status", "--short", "--untracked-files=all")
    patch = git_output(worktree, "diff", "--binary", base_sha)
    summary = git_output(worktree, "diff", "--stat", base_sha)
    paths = collect_changed_paths(worktree, base_sha)
    for relative in paths:
        if "\n?? " + relative in "\n" + status or status.startswith("?? " + relative):
            patch += _untracked_patch(worktree, relative)
    return status, patch, summary


class ProposalWorkspace:
    """同じ Proposal の implementation/repair Run が共有する workspace。"""

    def __init__(self, *, manifest: RunManifest, run_dir: Path) -> None:
        if manifest.proposal_id is None:
            raise WorkspaceError("Proposal workspace には proposal RunManifest が必要です")
        self.manifest = manifest
        self.run_dir = run_dir.resolve(strict=False)
        self.descriptor_path = self.run_dir / "workspace.json"
        self._descriptor: WorkspaceDescriptor | None = None

    @property
    def worktree_path(self) -> Path:
        return self.manifest.worktree_path

    @property
    def descriptor(self) -> WorkspaceDescriptor:
        if self._descriptor is None:
            if not self.descriptor_path.exists():
                raise WorkspaceError("workspace descriptor がありません")
            self._descriptor = WorkspaceDescriptor.load(self.descriptor_path)
        return self._descriptor

    @property
    def status(self) -> WorkspaceStatus:
        return self.descriptor.status

    def _assert_descriptor_matches(self, descriptor: WorkspaceDescriptor) -> None:
        expected = WorkspaceDescriptor.from_manifest(self.manifest, status=descriptor.status)
        for field in ("proposal_id", "repository", "base_sha", "branch", "worktree_path"):
            if getattr(descriptor, field) != getattr(expected, field):
                raise WorkspaceError(f"workspace descriptor の {field} が manifest と不一致です")
        if descriptor.status is WorkspaceStatus.CLOSED:
            raise WorkspaceError("closed workspace は再利用できません")

    def _registry_entry(self) -> GitWorktreeInfo | None:
        expected = self.worktree_path.resolve(strict=False)
        return next((item for item in list_worktrees(self.manifest.repository) if item.path == expected), None)

    def _assert_registry(self, *, allow_descendant: bool) -> GitWorktreeInfo:
        entry = self._registry_entry()
        if entry is None:
            raise WorkspaceError("worktree が Git registry にありません")
        expected_branch = f"refs/heads/{self.manifest.branch}"
        if entry.branch != expected_branch:
            raise WorkspaceError(f"registered branch が不一致です: {entry.branch!r}")
        if not allow_descendant and entry.head != self.manifest.base_sha:
            raise WorkspaceError("worktree HEAD が base_sha と一致しません")
        if allow_descendant:
            result = git_output(self.manifest.repository, "merge-base", "--is-ancestor", self.manifest.base_sha, entry.head, check=False)
            if result is None:  # pragma: no cover - git_output は常に str を返す
                raise WorkspaceError("base_sha の ancestry を検証できません")
            # --is-ancestor は stdout を返さないため、再度 return code を確認する。
            try:
                subprocess.run(
                    ["git", "merge-base", "--is-ancestor", self.manifest.base_sha, entry.head],
                    cwd=str(self.manifest.repository), check=True, capture_output=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise WorkspaceError("worktree HEAD が base_sha の descendant ではありません") from exc
        return entry

    def create(self) -> Path:
        if self.descriptor_path.exists():
            return self.adopt_existing()
        existing_path = self._registry_entry()
        branch_ref = f"refs/heads/{self.manifest.branch}"
        branch_exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", branch_ref],
            cwd=str(self.manifest.repository), capture_output=True, check=False,
        ).returncode == 0
        if existing_path is not None or branch_exists:
            raise WorkspaceError("Proposal workspace の path または branch が既に存在します")
        self.worktree_path.parent.mkdir(parents=True, exist_ok=True)
        git_output(
            self.manifest.repository,
            "worktree", "add", "-b", self.manifest.branch,
            str(self.worktree_path), self.manifest.base_sha,
        )
        self._descriptor = WorkspaceDescriptor.from_manifest(self.manifest, status=WorkspaceStatus.READY)
        _write_descriptor(self.descriptor_path, self._descriptor)
        return self.worktree_path

    def adopt_existing(self) -> Path:
        descriptor = WorkspaceDescriptor.load(self.descriptor_path) if self.descriptor_path.exists() else None
        if descriptor is not None:
            self._assert_descriptor_matches(descriptor)
        entry = self._assert_registry(allow_descendant=True)
        del entry
        self._descriptor = descriptor or WorkspaceDescriptor.from_manifest(self.manifest, status=WorkspaceStatus.READY)
        if descriptor is None:
            _write_descriptor(self.descriptor_path, self._descriptor)
        return self.worktree_path

    def acquire(self) -> Path:
        if not self.descriptor_path.exists():
            self.create()
        else:
            self.adopt_existing()
        self._descriptor = WorkspaceDescriptor.from_manifest(self.manifest, status=WorkspaceStatus.IN_USE)
        _write_descriptor(self.descriptor_path, self._descriptor)
        return self.worktree_path

    def snapshot(self) -> Path:
        """patch と監査情報だけ保存し、worktree は保持する。"""

        self.adopt_existing()
        status, patch, summary = _worktree_patch(self.worktree_path, self.manifest.base_sha)
        changed_paths = list(collect_changed_paths(self.worktree_path, self.manifest.base_sha))
        artifact_dir = self.run_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        patch_path = artifact_dir / "candidate.patch"
        patch_path.write_text(patch, encoding="utf-8")
        (artifact_dir / "git-status.txt").write_text(status, encoding="utf-8")
        (artifact_dir / "git-diff-summary.txt").write_text(summary, encoding="utf-8")
        audit = {
            "schema_version": 1,
            "proposal_id": self.manifest.proposal_id,
            "base_sha": self.manifest.base_sha,
            "branch": self.manifest.branch,
            "worktree_path": str(self.worktree_path),
            "changed_paths": changed_paths,
            "candidate_diff_sha256": candidate_diff_sha256(self.worktree_path, self.manifest.base_sha),
        }
        (artifact_dir / "workspace-audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self._descriptor = WorkspaceDescriptor.from_manifest(self.manifest, status=WorkspaceStatus.SNAPSHOTTED)
        _write_descriptor(self.descriptor_path, self._descriptor)
        return patch_path

    def commit_candidate(self, *, gate_report: Any) -> CandidateCommit:
        """Proposal workspace が所有する controller-only commit 境界。"""

        self.adopt_existing()
        return CandidateCommitter(manifest=self.manifest, worktree=self.worktree_path).commit_candidate(
            gate_report=gate_report
        )

    def finalize(self, *, phase: ProposalPhase | str | None = None, terminal: bool = False) -> Path:
        """terminal または MERGE_READY のときだけ snapshot 後に worktree を削除する。"""

        if phase is not None:
            phase = ProposalPhase(phase)
        allowed = terminal or phase is ProposalPhase.MERGE_READY or phase in TERMINAL_PHASES
        if not allowed:
            raise WorkspaceError("Proposal workspace は terminal/MERGE_READY でのみ finalize できます")
        patch_path = self.snapshot()
        try:
            git_output(self.manifest.repository, "worktree", "remove", "--force", str(self.worktree_path))
        except Exception:
            # patch は既に保存済みなので、削除失敗時も workspace を再concile できる。
            raise
        self._descriptor = WorkspaceDescriptor.from_manifest(self.manifest, status=WorkspaceStatus.CLOSED)
        _write_descriptor(self.descriptor_path, self._descriptor)
        return patch_path


WorkspaceController = ProposalWorkspace
