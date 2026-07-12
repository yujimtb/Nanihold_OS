"""G1: deterministic diff-policy checks for a candidate worktree."""

from __future__ import annotations

import re
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

MAX_CHANGED_FILES = 400
MAX_CHANGED_LINES = 8000

# These are protected policy/specification paths.  They are intentionally
# constants in the trusted runner; a candidate cannot replace them via a
# configuration file.
_PROTECTED_ROOTS = (".github/", "vsm/gates/")
_PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN [^-\r\n]*PRIVATE KEY-----", re.IGNORECASE
)
_SECRET_KEY_PATTERN = re.compile(
    rb"(?i)(api|secret|token)[_-]?key\s*[:=]\s*['\"][A-Za-z0-9_-]{16,}['\"]"
)


class GateExecutionUnavailable(RuntimeError):
    """Raised when a gate cannot inspect or execute its required tooling."""


@dataclass(frozen=True)
class DiffSnapshot:
    """All source-control facts used by G1 and the frontend applicability check."""

    changed_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    added_content: bytes
    untracked_content: tuple[tuple[str, bytes], ...]
    diff_check_output: str
    diff_check_failed: bool
    changed_files: int
    changed_lines: int
    symlink_paths: tuple[str, ...]
    added_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyResult:
    """Result of the policy checks, before it is rendered into a report."""

    passed: bool
    summary: str
    highlights: tuple[str, ...]


def _write_log(log: TextIO, text: str) -> None:
    log.write(text)
    if not text.endswith("\n"):
        log.write("\n")
    log.flush()


def _git_environment() -> dict[str, str]:
    """Prevent a caller's Git worktree override from escaping into G1."""

    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("GIT_"):
            environment.pop(key, None)
    return environment


def _git(
    worktree: Path,
    args: list[str],
    *,
    log: TextIO,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    _write_log(log, "$ " + " ".join(command))
    try:
        completed = subprocess.run(
            command,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
            env=_git_environment(),
        )
    except OSError as exc:
        raise GateExecutionUnavailable(f"git を実行できません: {exc}") from exc
    if completed.stdout:
        _write_log(log, completed.stdout)
    if completed.stderr:
        _write_log(log, completed.stderr)
    if completed.returncode != 0:
        raise GateExecutionUnavailable(
            f"git {' '.join(args)} が終了コード {completed.returncode} で失敗しました"
        )
    return completed


def _relative_path(worktree: Path, path: str) -> Path:
    # Git pathnames are slash-separated even on Windows.
    return worktree.joinpath(*path.split("/"))


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _is_output_path(worktree: Path, path: str, output_root: Path | None) -> bool:
    if output_root is None:
        return False
    return _is_under(_relative_path(worktree, path), output_root)


def _parse_name_status(output: str) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        fields = line.split("\t", 1)
        if len(fields) == 2:
            paths.append((fields[0], fields[1]))
    return paths


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise GateExecutionUnavailable(f"差分ファイルを読めません: {path}: {exc}") from exc


def collect_diff_snapshot(
    worktree: Path,
    base: str,
    *,
    log_path: Path,
    output_root: Path | None = None,
) -> DiffSnapshot:
    """Collect the candidate diff once, so all gates use the same view."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        _write_log(log, f"worktree={worktree}")
        _write_log(log, f"base={base}")

        name_status = _git(
            worktree,
            ["diff", "--no-ext-diff", "--name-status", "--no-renames", base, "--"],
            log=log,
        )
        name_status_entries = _parse_name_status(name_status.stdout)
        changed_paths = {
            path
            for _, path in name_status_entries
            if not _is_output_path(worktree, path, output_root)
        }
        added_paths = {
            path
            for status, path in name_status_entries
            if status == "A" and not _is_output_path(worktree, path, output_root)
        }

        untracked_result = _git(
            worktree,
            # Omitting --exclude-standard includes ignored additions such as
            # .env files; those must be rejected by G1 as well.
            ["ls-files", "--others", "-z"],
            log=log,
        )
        untracked_paths = {
            path
            for path in untracked_result.stdout.split("\0")
            if path and not _is_output_path(worktree, path, output_root)
        }
        changed_paths.update(untracked_paths)
        added_paths.update(untracked_paths)

        numstat = _git(
            worktree,
            ["diff", "--no-ext-diff", "--numstat", base, "--"],
            log=log,
        )
        changed_lines = 0
        for line in numstat.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            try:
                added = int(fields[0])
                deleted = int(fields[1])
            except ValueError:
                # Binary files have '-' numstat fields and do not contribute
                # line counts, but they remain counted as changed files.
                continue
            changed_lines += added + deleted

        diff_text = _git(
            worktree,
            ["diff", "--no-ext-diff", "--unified=0", base, "--"],
            log=log,
        ).stdout
        added_content = b"\n".join(
            line[1:].encode("utf-8", errors="replace")
            for line in diff_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )

        try:
            diff_check = subprocess.run(
                ["git", "diff", "--no-ext-diff", "--check", base, "--"],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=False,
                env=_git_environment(),
            )
        except OSError as exc:
            raise GateExecutionUnavailable(
                f"git diff --check を実行できません: {exc}"
            ) from exc
        _write_log(log, "$ git diff --no-ext-diff --check " + base + " --")
        if diff_check.stdout:
            _write_log(log, diff_check.stdout)
        if diff_check.stderr:
            _write_log(log, diff_check.stderr)
        # Git versions use either 1 or 2 for detected whitespace errors.
        if diff_check.returncode not in (0, 1, 2):
            raise GateExecutionUnavailable(
                f"git diff --check を実行できません (終了コード {diff_check.returncode})"
            )

        summary = _git(
            worktree,
            ["diff", "--no-ext-diff", "--summary", base, "--"],
            log=log,
        ).stdout
        summary_has_symlink = "120000" in summary

        untracked_content: list[tuple[str, bytes]] = []
        symlink_paths: set[str] = set()
        for path in sorted(changed_paths):
            candidate = _relative_path(worktree, path)
            if candidate.is_symlink():
                symlink_paths.add(path)
            if path in untracked_paths and not candidate.is_symlink():
                content = _read_bytes(candidate)
                untracked_content.append((path, content))
                changed_lines += content.count(b"\n")
            elif candidate.exists() and not candidate.is_dir():
                # Read only changed files for the secret scan. Existing lines
                # outside the patch are not newly introduced by this change.
                pass

        if summary_has_symlink:
            symlink_paths.add("(deleted or replaced symlink in diff)")

        return DiffSnapshot(
            changed_paths=tuple(sorted(changed_paths)),
            untracked_paths=tuple(sorted(untracked_paths)),
            added_content=added_content,
            untracked_content=tuple(untracked_content),
            diff_check_output=(diff_check.stdout + diff_check.stderr).strip(),
            diff_check_failed=diff_check.returncode != 0,
            changed_files=len(changed_paths),
            changed_lines=changed_lines,
            symlink_paths=tuple(sorted(symlink_paths)),
            added_paths=tuple(sorted(added_paths)),
        )


def _is_protected_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized == "AGENTS.md" or normalized == "vsm.toml":
        return True
    if normalized.startswith(_PROTECTED_ROOTS):
        return True
    if not normalized.startswith("openspec/changes/"):
        return False
    name = normalized.rsplit("/", 1)[-1]
    if name == "tasks.md" or name.endswith("-result.md"):
        return False
    # OpenSpec sources are policy inputs.  Result notes and tasks are the
    # explicitly writable records for this workflow.
    return "/specs/" in f"/{normalized}" or name in {
        "spec.md",
        "proposal.md",
        "design.md",
    }


def _looks_like_env(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


def _secret_hits(content: bytes) -> list[str]:
    hits: list[str] = []
    if _PRIVATE_KEY_PATTERN.search(content):
        hits.append("private-key-header")
    if _SECRET_KEY_PATTERN.search(content):
        hits.append("secret-key-pattern")
    return hits


def evaluate_policy(snapshot: DiffSnapshot) -> PolicyResult:
    """Evaluate all G1 rules and return every finding, not only the first."""

    findings: list[str] = []
    protected = [path for path in snapshot.changed_paths if _is_protected_path(path)]
    if protected:
        findings.append("protected path changed: " + ", ".join(protected))
    if snapshot.untracked_paths:
        findings.append("untracked files: " + ", ".join(snapshot.untracked_paths))
    if snapshot.symlink_paths:
        findings.append("symbolic link changed: " + ", ".join(snapshot.symlink_paths))

    secret_hits: list[str] = []
    for hit in _secret_hits(snapshot.added_content):
        secret_hits.append(f"diff:{hit}")
    for path, content in snapshot.untracked_content:
        for hit in _secret_hits(content):
            secret_hits.append(f"{path}:{hit}")
    env_added = [path for path in snapshot.added_paths if _looks_like_env(path)]
    if env_added:
        secret_hits.extend(f"{path}:env-file" for path in env_added)
    if secret_hits:
        findings.append("secret-like content: " + ", ".join(secret_hits))

    if snapshot.changed_files > MAX_CHANGED_FILES or snapshot.changed_lines > MAX_CHANGED_LINES:
        findings.append(
            "diff too large (warning then reject): "
            f"files={snapshot.changed_files} (limit {MAX_CHANGED_FILES}), "
            f"lines={snapshot.changed_lines} (limit {MAX_CHANGED_LINES})"
        )
    if snapshot.diff_check_failed:
        detail = snapshot.diff_check_output or "whitespace error"
        findings.append("git diff --check failed: " + detail)

    if findings:
        return PolicyResult(
            passed=False,
            summary="; ".join(findings),
            highlights=tuple(findings),
        )
    return PolicyResult(
        passed=True,
        summary=(
            f"diff policy passed: {snapshot.changed_files} files, "
            f"{snapshot.changed_lines} changed lines"
        ),
        highlights=("all G1 checks passed",),
    )


def run_g1(
    worktree: Path,
    base: str,
    *,
    log_path: Path,
    output_root: Path | None = None,
) -> tuple[DiffSnapshot, PolicyResult]:
    """Collect and evaluate G1, returning the snapshot for G3 reuse."""

    snapshot = collect_diff_snapshot(
        worktree,
        base,
        log_path=log_path,
        output_root=output_root,
    )
    result = evaluate_policy(snapshot)
    with log_path.open("a", encoding="utf-8") as log:
        _write_log(log, "")
        _write_log(log, "G1 result: " + result.summary)
    return snapshot, result
