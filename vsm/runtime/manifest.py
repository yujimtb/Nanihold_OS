"""Self-hosting Run の manifest と隔離 worktree 管理。

Self-hosting の書き込み境界は、AI のプロンプトではなくこのモジュールが
所有する。manifest は Run の監査記録として ``manifest.json`` に保存し、
``WorkspaceController`` は manifest の base SHA から専用 branch/worktree を
作成する。終了時には worktree 上の差分を Run directory に退避してから
worktree を登録解除する。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from vsm.errors import WorkspaceError, WorkspacePolicyError

__all__ = [
    "DEFAULT_SELFDEV_FORBIDDEN_PATHS",
    "RunManifest",
    "GitWorktree",
    "WorkspaceController",
    "find_orphan_worktrees",
]


DEFAULT_SELFDEV_FORBIDDEN_PATHS = (
    "AGENTS.md",
    ".github/",
    "vsm.toml",
    "openspec/",
)


def _normalise_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path は空でない文字列でなければなりません")
    normalised = value.strip().replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    if not normalised or normalised == "." or normalised.startswith("/"):
        raise ValueError(f"repository-relative path が不正です: {value!r}")
    if any(part == ".." for part in normalised.split("/")):
        raise ValueError(f"repository-relative path は親を参照できません: {value!r}")
    return normalised.rstrip("/")


def _path_matches(path: str, rule: str) -> bool:
    return path == rule or path.startswith(f"{rule}/")


@dataclass(frozen=True, slots=True)
class RunManifest:
    """1 Run の self-hosting 実行契約と監査メタデータ。

    ``issued_by`` は ``{"decision": "...", "conversation": "..."}``
    のような参照マップを想定する。値を文字列に限定することで、manifest
    を JSON へ永続化した際に発行元を曖昧にしない。
    """

    run_id: str
    repository: Path
    base_sha: str
    worktree_path: Path
    backend: str = ""
    model: str = ""
    budget: Mapping[str, Any] = field(default_factory=dict)
    risk_class: str = ""
    issued_by: Mapping[str, str] = field(default_factory=dict)
    schema_version: int = 1
    proposal_id: str | None = None
    attempt: int = 1
    run_kind: str = "implementation"
    parent_run_id: str | None = None
    branch: str = ""
    allowed_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = DEFAULT_SELFDEV_FORBIDDEN_PATHS
    proposal_manifest_ref: str | None = None
    proposal_manifest_sha256: str | None = None
    scope: tuple[Mapping[str, Any], ...] = ()
    scope_sha256: str | None = None
    acceptance_criteria: tuple[Any, ...] = ()
    required_gates: tuple[str, ...] = ()
    writer_runtime: Mapping[str, Any] | None = None
    analysis_runtime: Mapping[str, Any] | None = None
    initial_decision_event_id: str | None = None
    protected_approval_event_id: str | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_paths", tuple(self.allowed_paths))
        object.__setattr__(self, "forbidden_paths", tuple(self.forbidden_paths))
        object.__setattr__(self, "required_gates", tuple(self.required_gates))
        object.__setattr__(self, "scope", tuple(self.scope))
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id は空にできません")
        if any(char in self.run_id for char in "/\\"):
            raise ValueError("run_id はパス区切りを含められません")
        if not isinstance(self.repository, Path):
            raise TypeError("repository は pathlib.Path でなければなりません")
        if not isinstance(self.worktree_path, Path):
            raise TypeError("worktree_path は pathlib.Path でなければなりません")
        if self.schema_version != 1:
            raise ValueError("RunManifest schema_version は1固定です")
        if not isinstance(self.base_sha, str) or not self.base_sha.strip():
            raise ValueError("base_sha は空にできません")
        new_contract = self.proposal_id is not None
        if new_contract:
            if not isinstance(self.proposal_id, str) or not __import__("re").fullmatch(
                r"proposal-[0-9a-f]{32}", self.proposal_id
            ):
                raise ValueError("proposal_id は proposal-<32hex> でなければなりません")
            if self.attempt not in (1, 2):
                raise ValueError("attempt は1または2でなければなりません")
            if self.run_kind not in {"implementation", "repair"}:
                raise ValueError("run_kind は implementation または repair でなければなりません")
            if self.run_kind == "repair" and (self.attempt != 2 or not self.parent_run_id):
                raise ValueError("repair Run には attempt=2 と parent_run_id が必要です")
            if self.run_kind == "implementation" and (self.attempt != 1 or self.parent_run_id is not None):
                raise ValueError("implementation Run の attempt/parent_run_id が不正です")
            expected_branch = f"selfdev/{self.proposal_id}"
        else:
            expected_branch = f"selfdev/{self.run_id}"
        branch = self.branch or expected_branch
        if branch != expected_branch:
            raise ValueError(
                f"self-hosting branch は {expected_branch!r} でなければなりません"
            )
        for name in ("backend", "model", "risk_class"):
            value = getattr(self, name)
            if not new_contract and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} は空にできません")
        if new_contract:
            if self.risk_class not in {"low", "normal", "protected"}:
                raise ValueError("risk_class が不正です")
            if self.required_gates != ("g1", "g2", "g3", "g4"):
                raise ValueError("required_gates は g1,g2,g3,g4 固定です")
            if not self.proposal_manifest_ref or not self.proposal_manifest_sha256:
                raise ValueError("proposal manifest ref/hash は必須です")
            if not __import__("re").fullmatch(r"[0-9a-f]{64}", self.proposal_manifest_sha256):
                raise ValueError("proposal_manifest_sha256 が不正です")
            if not self.scope or not self.scope_sha256:
                raise ValueError("scope と scope_sha256 は必須です")
            if self.writer_runtime is None:
                raise ValueError("writer_runtime は必須です")
            if not self.initial_decision_event_id:
                raise ValueError("initial_decision_event_id は必須です")
            if self.risk_class == "protected" and not self.protected_approval_event_id:
                raise ValueError("protected Run には protected_approval_event_id が必要です")
        if not isinstance(self.budget, Mapping):
            raise TypeError("budget はマッピングでなければなりません")
        if not new_contract and (not isinstance(self.issued_by, Mapping) or not self.issued_by):
            raise ValueError("issued_by は空でない参照マップでなければなりません")
        issuer = dict(self.issued_by)
        if not new_contract and any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in issuer.items()
        ):
            raise ValueError("issued_by のキーと値は空でない文字列でなければなりません")
        if not new_contract and (not ({"decision", "decision_ref"} & issuer.keys()) or not (
            {"conversation", "conversation_ref"} & issuer.keys()
        )):
            raise ValueError(
                "issued_by は decision と conversation の参照を含まなければなりません"
            )

        normalised_allowed = tuple(
            _normalise_relative_path(value) for value in self.allowed_paths
        )
        normalised_forbidden = tuple(
            _normalise_relative_path(value) for value in self.forbidden_paths
        )
        for field_name in ("required_gates",):
            values = getattr(self, field_name)
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{field_name} は空でない文字列の列でなければなりません")

        object.__setattr__(self, "repository", self.repository.resolve(strict=False))
        object.__setattr__(self, "worktree_path", self.worktree_path.resolve(strict=False))
        object.__setattr__(self, "branch", branch)
        object.__setattr__(self, "budget", dict(self.budget))
        object.__setattr__(self, "issued_by", issuer)
        object.__setattr__(self, "allowed_paths", normalised_allowed)
        object.__setattr__(self, "forbidden_paths", normalised_forbidden)
        object.__setattr__(self, "acceptance_criteria", tuple(self.acceptance_criteria))
        object.__setattr__(self, "required_gates", tuple(self.required_gates))
        object.__setattr__(self, "scope", tuple(dict(item) for item in self.scope))
        if new_contract and self.forbidden_paths == DEFAULT_SELFDEV_FORBIDDEN_PATHS:
            object.__setattr__(self, "forbidden_paths", ())
        if new_contract and not normalised_allowed:
            object.__setattr__(
                self,
                "allowed_paths",
                tuple(_normalise_relative_path(str(item["path"])) for item in self.scope),
            )

    def to_dict(self) -> dict[str, Any]:
        """JSON 永続化用の plain dict を返す。"""

        legacy = self.proposal_id is None
        if legacy:
            return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "repository": str(self.repository),
            "base_sha": self.base_sha,
            "branch": self.branch,
            "worktree_path": str(self.worktree_path),
            "allowed_paths": list(self.allowed_paths),
            "forbidden_paths": list(self.forbidden_paths),
            "acceptance_criteria": list(self.acceptance_criteria),
            "required_gates": list(self.required_gates),
            "backend": self.backend,
            "model": self.model,
            "budget": dict(self.budget),
            "risk_class": self.risk_class,
            "issued_by": dict(self.issued_by),
            }
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "proposal_id": self.proposal_id,
            "attempt": self.attempt,
            "run_kind": self.run_kind,
            "parent_run_id": self.parent_run_id,
            "repository": str(self.repository),
            "base_sha": self.base_sha,
            "branch": self.branch,
            "worktree_path": str(self.worktree_path),
            "proposal_manifest_ref": self.proposal_manifest_ref,
            "proposal_manifest_sha256": self.proposal_manifest_sha256,
            "scope": [dict(item) for item in self.scope],
            "scope_sha256": self.scope_sha256,
            "acceptance_criteria": list(self.acceptance_criteria),
            "required_gates": list(self.required_gates),
            "writer_runtime": dict(self.writer_runtime or {}),
            "analysis_runtime": dict(self.analysis_runtime) if self.analysis_runtime else None,
            "budget": dict(self.budget),
            "risk_class": self.risk_class,
            "initial_decision_event_id": self.initial_decision_event_id,
            "protected_approval_event_id": self.protected_approval_event_id,
            "created_at": self.created_at,
        }

    def persist(self, run_dir: Path) -> Path:
        """Run directory に ``manifest.json`` を作成して返す。"""

        target_dir = run_dir.resolve(strict=False)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "manifest.json"
        try:
            payload = json.dumps(
                self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"manifest を JSON 化できません: {exc}") from exc
        target.write_text(f"{payload}\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, run_dir: Path) -> "RunManifest":
        """Run directory の ``manifest.json`` を読み込む。"""

        target = run_dir / "manifest.json"
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"manifest を読み込めません: {target}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"manifest の root は object でなければなりません: {target}")
        if payload.get("schema_version") != 1:
            raise ValueError("unversioned または未知の RunManifest は拒否します")
        for field_name in ("repository", "worktree_path"):
            if field_name in payload:
                payload[field_name] = Path(payload[field_name])
        for field_name in (
            "allowed_paths",
            "forbidden_paths",
            "acceptance_criteria",
            "required_gates",
            "scope",
        ):
            if field_name in payload:
                payload[field_name] = tuple(payload[field_name])
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class GitWorktree:
    """``git worktree list --porcelain`` の必要部分。"""

    path: Path
    head: str
    branch: str | None


def _run_git(repository: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise WorkspaceError(f"git {' '.join(args)} に失敗しました: {detail.strip()}") from exc
    return completed.stdout


def _run_untracked_diff(worktree: Path, relative_path: str) -> str:
    """untracked file を ``/dev/null`` との差分として取得する。"""

    try:
        completed = subprocess.run(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", relative_path],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise WorkspaceError(f"untracked diff の取得に失敗しました: {exc}") from exc
    if completed.returncode not in (0, 1):
        raise WorkspaceError(
            f"untracked diff の取得に失敗しました: {completed.stderr.strip()}"
        )
    return completed.stdout


def _parse_worktree_list(output: str) -> list[GitWorktree]:
    entries: list[GitWorktree] = []
    current: dict[str, str] = {}
    for line in output.splitlines() + [""]:
        if line:
            key, _, value = line.partition(" ")
            if key:
                current[key] = value
            continue
        if current.get("worktree") and current.get("HEAD"):
            entries.append(
                GitWorktree(
                    path=Path(current["worktree"]).resolve(strict=False),
                    head=current["HEAD"],
                    branch=current.get("branch"),
                )
            )
        current = {}
    return entries


class WorkspaceController:
    """manifest に対応する branch/worktree のライフサイクルを管理する。"""

    def __init__(self, *, manifest: RunManifest, run_dir: Path) -> None:
        self.manifest = manifest
        self.run_dir = run_dir.resolve(strict=False)
        self._created = False
        self._finalized = False

    @property
    def worktree_path(self) -> Path:
        return self.manifest.worktree_path

    def create_worktree(self) -> Path:
        """base SHA から専用 branch と worktree を作成する。"""

        if self._finalized:
            raise WorkspaceError("finalize 済みの WorkspaceController は再利用できません")
        if self._created:
            return self.worktree_path
        self.manifest.persist(self.run_dir)
        self.worktree_path.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            self.manifest.repository,
            "worktree",
            "add",
            "-b",
            self.manifest.branch,
            str(self.worktree_path),
            self.manifest.base_sha,
        )
        self._created = True
        return self.worktree_path

    def start(self) -> Path:
        """Run 開始時の worktree 作成入口。"""

        return self.create_worktree()

    @staticmethod
    def list_worktrees(repository: Path) -> list[GitWorktree]:
        return _parse_worktree_list(_run_git(repository, "worktree", "list", "--porcelain"))

    @staticmethod
    def find_orphan_worktrees(
        repository: Path,
        expected: Iterable[RunManifest | Path],
    ) -> list[Path]:
        expected_paths = {
            (
                item.worktree_path
                if isinstance(item, RunManifest)
                else item
            ).resolve(strict=False)
            for item in expected
        }
        return [
            entry.path
            for entry in WorkspaceController.list_worktrees(repository)
            if entry.branch is not None
            and entry.branch.removeprefix("refs/heads/").startswith("selfdev/")
            and entry.path not in expected_paths
        ]

    def _collect(self) -> tuple[str, str, str, list[str]]:
        status = _run_git(
            self.worktree_path,
            "status",
            "--short",
            "--untracked-files=all",
        )
        patch = _run_git(
            self.worktree_path,
            "diff",
            "--binary",
            self.manifest.base_sha,
        )
        diff_summary = _run_git(
            self.worktree_path,
            "diff",
            "--stat",
            self.manifest.base_sha,
        )
        paths: list[str] = []
        untracked_paths: list[str] = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            value = line[3:].strip()
            if " -> " in value:
                value = value.rsplit(" -> ", 1)[1]
            normalised = _normalise_relative_path(value)
            paths.append(normalised)
            if line.startswith("??"):
                untracked_paths.append(normalised)
        for untracked_path in untracked_paths:
            patch += _run_untracked_diff(self.worktree_path, untracked_path)
        if untracked_paths:
            diff_summary = (
                f"{diff_summary.rstrip()}\n"
                "Untracked files:\n"
                + "\n".join(f" {path}" for path in untracked_paths)
                + "\n"
            )
        return status, patch, diff_summary, paths

    def finalize(self) -> Path:
        """差分を保存し、worktree を削除する。"""

        if self._finalized:
            return self.run_dir / "candidate.patch"
        if not self._created:
            raise WorkspaceError("worktree が作成されていないため finalize できません")

        status, patch, diff_summary, changed_paths = self._collect()
        forbidden = [
            path
            for path in changed_paths
            if any(_path_matches(path, rule) for rule in self.manifest.forbidden_paths)
        ]
        outside_allowed = [
            path
            for path in changed_paths
            if self.manifest.allowed_paths
            and not any(_path_matches(path, rule) for rule in self.manifest.allowed_paths)
        ]
        violations = {
            "forbidden_paths": forbidden,
            "outside_allowed_paths": outside_allowed,
        }

        self.run_dir.mkdir(parents=True, exist_ok=True)
        candidate_path = self.run_dir / "candidate.patch"
        status_path = self.run_dir / "git-status.txt"
        summary_path = self.run_dir / "git-diff-summary.txt"
        audit_path = self.run_dir / "workspace-audit.json"
        candidate_path.write_text(patch, encoding="utf-8")
        status_path.write_text(status, encoding="utf-8")
        summary_path.write_text(diff_summary, encoding="utf-8")
        audit_path.write_text(
            json.dumps(
                {
                    "run_id": self.manifest.run_id,
                    "base_sha": self.manifest.base_sha,
                    "branch": self.manifest.branch,
                    "worktree_path": str(self.worktree_path),
                    "changed_paths": changed_paths,
                    "policy_violations": violations,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        cleanup_error: Exception | None = None
        try:
            _run_git(
                self.manifest.repository,
                "worktree",
                "remove",
                "--force",
                str(self.worktree_path),
            )
        except Exception as exc:
            cleanup_error = exc
        self._finalized = cleanup_error is None
        if cleanup_error is not None:
            raise cleanup_error
        if forbidden or outside_allowed:
            raise WorkspacePolicyError(
                "manifest の変更境界違反: "
                + json.dumps(violations, ensure_ascii=False, sort_keys=True)
            )
        return candidate_path

    def interrupt(self) -> Path:
        """中断時も finalize と同じ監査・差分退避を行う。"""

        return self.finalize()

    def discard(self) -> None:
        """Run 開始失敗時に監査対象にせず作成済み worktree を破棄する。"""

        if self._finalized or not self._created:
            return
        _run_git(
            self.manifest.repository,
            "worktree",
            "remove",
            "--force",
            str(self.worktree_path),
        )
        self._finalized = True


def find_orphan_worktrees(
    repository: Path,
    expected: Iterable[RunManifest | Path],
) -> list[Path]:
    """登録済み manifest と git worktree 一覧を照合して孤児を返す。"""

    return WorkspaceController.find_orphan_worktrees(repository, expected)
