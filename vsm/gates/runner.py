"""Trusted GateRunner command line and gate orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from vsm.gates.policy import (
    DiffSnapshot,
    GateExecutionUnavailable,
    collect_diff_snapshot,
    evaluate_policy,
)
from vsm.selfdev.verification import ProtectedApproval, REQUIRED_GATES, scope_sha256 as calculate_scope_sha256

DEFAULT_GATES: tuple[str, ...] = ("g1", "g2", "g3", "g4")

# Gate commands are deliberately code constants.  They are not read from
# vsm.toml, environment variables, or the candidate worktree.
G2_COMMAND: tuple[str, ...] = (
    "docker",
    "compose",
    "run",
    "--rm",
    "--no-deps",
    "-u",
    "root",
    "app",
    "sh",
    "-c",
    "python -m pip install -q -e '.[dev]' && python -m pytest -q",
)
G3_LINT_COMMAND: tuple[str, ...] = ("npm", "run", "lint")
G3_BUILD_COMMAND: tuple[str, ...] = ("npm", "run", "build")
G4_HELP_COMMAND: tuple[str, ...] = ("python", "-m", "vsm", "--help")
G4_SMOKE_COMMAND: tuple[str, ...] = ("python", "scripts/smoke_run.py")


@dataclass(frozen=True)
class GateResult:
    status: str
    duration_ms: int
    summary: str
    highlights: tuple[str, ...]
    log_path: Path
    execution_unavailable: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
            "highlights": list(self.highlights),
            "log_path": str(self.log_path.resolve()),
        }


class GateInputError(ValueError):
    """Raised for invalid runner arguments or an invalid candidate worktree."""


def _command_text(command: Sequence[str]) -> str:
    return " ".join(command)


def _write_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(text)
        if not text.endswith("\n"):
            log.write("\n")


def _trusted_environment() -> dict[str, str]:
    """candidate の同名 module より control-plane を優先する環境。"""

    environment = dict(os.environ)
    control_root = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = control_root
    environment["PYTHONNOUSERSITE"] = "1"
    environment["NANIHOLD_TRUSTED_GATE_RUNNER"] = "1"
    return environment


def _run_command(
    command: Sequence[str], *, cwd: Path, log_path: Path, trusted: bool = False
) -> int:
    _write_log(log_path, "$ " + _command_text(command))
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            env=_trusted_environment() if trusted else None,
        )
    except OSError as exc:
        _write_log(log_path, f"execution error: {exc}")
        raise GateExecutionUnavailable(
            f"コマンドを実行できません: {_command_text(command)}: {exc}"
        ) from exc
    if completed.stdout:
        _write_log(log_path, completed.stdout)
    if completed.stderr:
        _write_log(log_path, completed.stderr)
    _write_log(log_path, f"exit_code={completed.returncode}")
    return completed.returncode


def _run_g2(worktree: Path, log_path: Path, *, trusted: bool = False) -> tuple[str, str, tuple[str, ...]]:
    kwargs = {"cwd": worktree, "log_path": log_path}
    if trusted:
        kwargs["trusted"] = True  # type: ignore[assignment]
    returncode = _run_command(G2_COMMAND, **kwargs)  # type: ignore[arg-type]
    if returncode == 0:
        return "pass", "Docker Compose app の pytest が成功しました", ("pytest exit code 0",)
    return "fail", f"Docker Compose app の pytest が失敗しました (exit={returncode})", (
        f"pytest exit code {returncode}",
    )


def _frontend_changed(changed_paths: Iterable[str]) -> bool:
    return any(
        path == "frontend" or path.replace("\\", "/").startswith("frontend/")
        for path in changed_paths
    )


def _run_g3(
    worktree: Path,
    changed_paths: Iterable[str],
    log_path: Path,
    *,
    trusted: bool = False,
) -> tuple[str, str, tuple[str, ...]]:
    if not _frontend_changed(changed_paths):
        _write_log(log_path, "frontend change not detected; G3 skipped")
        return "skip", "frontend/ に変更がないため skip", ("frontend change not detected",)

    package_path = worktree / "frontend" / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateExecutionUnavailable(
            f"frontend/package.json を読めません: {exc}"
        ) from exc
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        raise GateExecutionUnavailable("frontend/package.json の scripts がありません")

    frontend_dir = worktree / "frontend"
    findings: list[str] = []
    if "lint" in scripts:
        kwargs = {"cwd": frontend_dir, "log_path": log_path}
        if trusted:
            kwargs["trusted"] = True  # type: ignore[assignment]
        lint_code = _run_command(G3_LINT_COMMAND, **kwargs)  # type: ignore[arg-type]
        if lint_code != 0:
            findings.append(f"lint exit code {lint_code}")
    kwargs = {"cwd": frontend_dir, "log_path": log_path}
    if trusted:
        kwargs["trusted"] = True  # type: ignore[assignment]
    build_code = _run_command(G3_BUILD_COMMAND, **kwargs)  # type: ignore[arg-type]
    if build_code != 0:
        findings.append(f"build exit code {build_code}")
    if findings:
        return "fail", "frontend gate failed: " + ", ".join(findings), tuple(findings)
    commands = ["npm run lint"] if "lint" in scripts else []
    commands.append("npm run build")
    return "pass", "frontend gate が成功しました", tuple(commands)


def _run_g4(worktree: Path, log_path: Path, *, trusted: bool = False) -> tuple[str, str, tuple[str, ...]]:
    kwargs = {"cwd": worktree, "log_path": log_path}
    if trusted:
        kwargs["trusted"] = True  # type: ignore[assignment]
    help_code = _run_command(G4_HELP_COMMAND, **kwargs)  # type: ignore[arg-type]
    if help_code != 0:
        return "fail", f"python -m vsm --help が失敗しました (exit={help_code})", (
            f"help exit code {help_code}",
        )
    kwargs = {"cwd": worktree, "log_path": log_path}
    if trusted:
        kwargs["trusted"] = True  # type: ignore[assignment]
    smoke_code = _run_command(G4_SMOKE_COMMAND, **kwargs)  # type: ignore[arg-type]
    if smoke_code != 0:
        return "fail", f"Fake 設定の mini Run が失敗しました (exit={smoke_code})", (
            f"smoke exit code {smoke_code}",
        )
    return "pass", "vsm --help と Fake 設定の mini Run が成功しました", (
        "help exit code 0",
        "smoke exit code 0",
    )


def _normalise_gates(gates: str | Iterable[str]) -> tuple[str, ...]:
    values = gates.split(",") if isinstance(gates, str) else list(gates)
    normalised = tuple(value.strip().lower() for value in values)
    if not normalised or any(not value for value in normalised):
        raise GateInputError("--gates は空にできません")
    unknown = sorted(set(normalised) - set(DEFAULT_GATES))
    if unknown:
        raise GateInputError("未知の gate: " + ", ".join(unknown))
    if len(set(normalised)) != len(normalised):
        raise GateInputError("同じ gate を複数回指定できません")
    return normalised


def _resolve_output(out: Path | None, worktree: Path) -> tuple[Path, Path]:
    if out is None:
        output_root = worktree / "runs" / "gate-runner"
        return output_root, output_root / "gate_report.json"
    resolved = out.resolve(strict=False)
    if resolved.suffix.lower() == ".json":
        return resolved.parent, resolved
    return resolved, resolved / "gate_report.json"


def _unavailable_result(log_path: Path, detail: str, started: float) -> GateResult:
    _write_log(log_path, "execution unavailable: " + detail)
    return GateResult(
        status="skip",
        duration_ms=int((time.monotonic() - started) * 1000),
        summary="実行不能: " + detail,
        highlights=(detail,),
        log_path=log_path,
        execution_unavailable=True,
    )


def _run_one(
    name: str,
    *,
    log_path: Path,
    callback,
) -> GateResult:
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    try:
        status, summary, highlights = callback()
    except GateExecutionUnavailable as exc:
        return _unavailable_result(log_path, str(exc), started)
    except Exception as exc:
        return _unavailable_result(log_path, f"{name} の実行中に予期しないエラー: {exc}", started)
    return GateResult(
        status=status,
        duration_ms=int((time.monotonic() - started) * 1000),
        summary=summary,
        highlights=tuple(highlights),
        log_path=log_path,
    )


def run(
    worktree: Path,
    *,
    base: str | None = None,
    gates: str | Iterable[str] = DEFAULT_GATES,
    out: Path | None = None,
    proposal_id: str | None = None,
    implementation_run_id: str | None = None,
    gate_attempt: int = 1,
    scope: Iterable[dict[str, object]] | None = None,
    scope_sha256: str | None = None,
    risk_class: str | None = None,
    proposal_manifest_sha256: str | None = None,
    protected_scope_sha256: str | None = None,
    protected_approval: ProtectedApproval | dict[str, object] | None = None,
    protected_approval_event_id: str | None = None,
) -> tuple[dict[str, object], int]:
    """Run the selected trusted gates and write ``gate_report.json``."""

    if proposal_id is not None or implementation_run_id is not None or scope is not None:
        return run_v2(
            worktree,
            base=base,
            out=out,
            gates=gates,
            proposal_id=proposal_id,
            implementation_run_id=implementation_run_id,
            gate_attempt=gate_attempt,
            scope=scope,
            scope_sha256=scope_sha256,
            risk_class=risk_class,
            proposal_manifest_sha256=proposal_manifest_sha256,
            protected_scope_sha256=protected_scope_sha256,
            protected_approval=protected_approval,
            protected_approval_event_id=protected_approval_event_id,
        )

    candidate = worktree.resolve(strict=False)
    if not candidate.exists() or not candidate.is_dir():
        raise GateInputError(f"worktree がディレクトリではありません: {worktree}")
    selected = _normalise_gates(gates)
    base_ref = base if base is not None else "HEAD"
    output_root, report_path = _resolve_output(out, candidate)
    output_root.mkdir(parents=True, exist_ok=True)
    logs_root = output_root / "logs"

    snapshot: DiffSnapshot | None = None
    snapshot_error: str | None = None
    if "g1" in selected or "g3" in selected:
        try:
            snapshot = collect_diff_snapshot(
                candidate,
                base_ref,
                log_path=logs_root / "g1.log",
                output_root=output_root,
            )
        except GateExecutionUnavailable as exc:
            snapshot_error = str(exc)
            _write_log(logs_root / "g1.log", "execution unavailable: " + snapshot_error)

    results: dict[str, GateResult] = {}
    for name in selected:
        log_path = logs_root / f"{name}.log"
        if name == "g1":
            if snapshot_error is not None:
                results[name] = _unavailable_result(log_path, snapshot_error, time.monotonic())
            else:
                assert snapshot is not None
                started = time.monotonic()
                result = evaluate_policy(snapshot)
                _write_log(log_path, "G1 result: " + result.summary)
                results[name] = GateResult(
                    status="pass" if result.passed else "fail",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    summary=result.summary,
                    highlights=result.highlights,
                    log_path=log_path,
                )
        elif name == "g2":
            results[name] = _run_one(
                name,
                log_path=log_path,
                callback=lambda: _run_g2(candidate, log_path),
            )
        elif name == "g3":
            if snapshot_error is not None:
                results[name] = _unavailable_result(log_path, snapshot_error, time.monotonic())
            else:
                assert snapshot is not None
                results[name] = _run_one(
                    name,
                    log_path=log_path,
                    callback=lambda: _run_g3(candidate, snapshot.changed_paths, log_path),
                )
        elif name == "g4":
            results[name] = _run_one(
                name,
                log_path=log_path,
                callback=lambda: _run_g4(candidate, log_path),
            )

    execution_error = any(result.execution_unavailable for result in results.values())
    failed = any(result.status == "fail" for result in results.values())
    exit_code = 2 if execution_error else 1 if failed else 0
    overall = "execution_error" if execution_error else "fail" if failed else "pass"
    report: dict[str, object] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "worktree": str(candidate),
        "base": base_ref,
        "gates_requested": list(selected),
        "status": overall,
        "exit_code": exit_code,
        "report_path": str(report_path.resolve()),
        "gates": {name: result.as_dict() for name, result in results.items()},
    }
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise GateExecutionUnavailable(f"gate report を保存できません: {exc}") from exc
    return report, exit_code


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise GateInputError(f"gate log を読めません: {path}: {exc}") from exc
    return digest.hexdigest()


def _v2_log_ref(output_root: Path, log_path: Path) -> str:
    """Proposal root から見た canonical な log ref を返す。"""

    # output_root is .../gates/attempt-N.  The caller may pass another
    # controller-owned directory, in which case the path remains explicit and
    # never becomes a candidate-worktree path.
    try:
        return log_path.resolve(strict=False).relative_to(output_root.parent.parent.resolve(strict=False)).as_posix()
    except ValueError:
        return log_path.name


def run_v2(
    worktree: Path,
    *,
    base: str | None,
    out: Path | None,
    gates: str | Iterable[str] = REQUIRED_GATES,
    proposal_id: str | None,
    implementation_run_id: str | None,
    gate_attempt: int,
    scope: Iterable[dict[str, object]] | None,
    scope_sha256: str | None,
    risk_class: str | None,
    proposal_manifest_sha256: str | None,
    protected_scope_sha256: str | None,
    protected_approval: ProtectedApproval | dict[str, object] | None,
    protected_approval_event_id: str | None,
) -> tuple[dict[str, object], int]:
    """Wave 2 strict runner。legacy ``run`` の省略契約とは分離する。"""

    candidate = worktree.resolve(strict=False)
    if not candidate.is_dir():
        raise GateInputError(f"worktree がディレクトリではありません: {worktree}")
    if proposal_id is None or implementation_run_id is None:
        raise GateInputError("v2 GateRunner には proposal_id と implementation_run_id が必要です")
    if gate_attempt not in (1, 2):
        raise GateInputError("gate_attempt は1または2でなければなりません")
    if scope is None or not scope_sha256:
        raise GateInputError("v2 GateRunner には scope と scope_sha256 が必要です")
    if not risk_class or not proposal_manifest_sha256:
        raise GateInputError("v2 GateRunner には risk_class と proposal_manifest_sha256 が必要です")
    if protected_approval_event_id is not None:
        if protected_approval is not None:
            raise GateInputError("protected approval は event object と event id を同時指定できません")
        if not protected_scope_sha256:
            raise GateInputError("protected approval event id には protected_scope_sha256 が必要です")
        protected_approval = {
            "event_id": protected_approval_event_id,
            "proposal_manifest_sha256": proposal_manifest_sha256,
            "protected_scope_sha256": protected_scope_sha256,
        }
    if out is None:
        raise GateInputError("v2 GateRunner の report/log 出力先は worktree 外で明示してください")
    selected = _normalise_gates(gates)
    if selected != REQUIRED_GATES:
        raise GateInputError("v2 GateRunner の gates は g1,g2,g3,g4 固定です")
    base_ref = base
    if not base_ref:
        raise GateInputError("v2 GateRunner には base を明示してください")
    output_root, report_path = _resolve_output(out, candidate)
    try:
        output_root.resolve(strict=False).relative_to(candidate)
    except ValueError:
        pass
    else:
        raise GateInputError("gate report/log は candidate worktree 外へ出力してください")
    output_root.mkdir(parents=True, exist_ok=True)
    logs_root = output_root / "logs"
    scope_rules = tuple(
        item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        for item in scope
    )
    if calculate_scope_sha256(scope_rules) != scope_sha256:
        raise GateInputError("scope_sha256 が scope の canonical hash と一致しません")

    try:
        snapshot = collect_diff_snapshot(
            candidate,
            base_ref,
            log_path=logs_root / "g1.log",
            output_root=output_root,
            scope=scope_rules,
            risk_class=risk_class,
            proposal_manifest_sha256=proposal_manifest_sha256,
            protected_scope_sha256=protected_scope_sha256,
            protected_approval=protected_approval,
        )
        snapshot_error: str | None = None
    except GateExecutionUnavailable as exc:
        snapshot = None
        snapshot_error = str(exc)
        _write_log(logs_root / "g1.log", "execution error: " + snapshot_error)

    results: dict[str, GateResult] = {}
    if snapshot_error is not None:
        started = time.monotonic()
        results["g1"] = GateResult("error", int((time.monotonic() - started) * 1000), snapshot_error, (snapshot_error,), logs_root / "g1.log", True)
        _write_log(logs_root / "g3.log", "execution error: " + snapshot_error)
        results["g3"] = GateResult("error", 0, snapshot_error, (snapshot_error,), logs_root / "g3.log", True)
    else:
        assert snapshot is not None
        started = time.monotonic()
        policy_result = evaluate_policy(snapshot)
        _write_log(logs_root / "g1.log", "G1 result: " + policy_result.summary)
        results["g1"] = GateResult(
            "pass" if policy_result.passed else "fail",
            int((time.monotonic() - started) * 1000),
            policy_result.summary,
            policy_result.highlights,
            logs_root / "g1.log",
        )
        results["g3"] = _run_one(
            "g3", log_path=logs_root / "g3.log",
            callback=lambda: _run_g3(candidate, snapshot.changed_paths, logs_root / "g3.log", trusted=True),
        )
    results["g2"] = _run_one(
        "g2", log_path=logs_root / "g2.log",
        callback=lambda: _run_g2(candidate, logs_root / "g2.log", trusted=True),
    )
    results["g4"] = _run_one("g4", log_path=logs_root / "g4.log", callback=lambda: _run_g4(candidate, logs_root / "g4.log", trusted=True))

    # Legacy GateResult maps an unavailable tool to skip.  v2 explicitly
    # distinguishes it as error in the report without changing old callers.
    has_error = any(result.execution_unavailable for result in results.values())
    has_failure = any(result.status == "fail" for result in results.values())
    overall = "error" if has_error else "fail" if has_failure else "pass"
    report_gates: dict[str, dict[str, object]] = {}
    for name in REQUIRED_GATES:
        result = results[name]
        report_gates[name] = {
            "status": "error" if result.execution_unavailable else result.status,
            "duration_ms": result.duration_ms,
            "summary": result.summary,
            "highlights": list(result.highlights),
            "log_ref": _v2_log_ref(output_root, result.log_path),
            "log_sha256": _sha256_path(result.log_path),
        }
    report: dict[str, object] = {
        "schema_version": 2,
        "proposal_id": proposal_id,
        "implementation_run_id": implementation_run_id,
        "gate_attempt": gate_attempt,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "worktree_path": str(candidate),
        "report_ref": _v2_log_ref(output_root, report_path),
        "base_sha": base_ref,
        "scope_sha256": scope_sha256,
        "candidate_diff_sha256": snapshot.diff_sha256 if snapshot is not None else "0" * 64,
        "gates_requested": list(REQUIRED_GATES),
        "status": overall,
        "exit_code": 2 if has_error else 1 if has_failure else 0,
        "changed_paths": list(snapshot.changed_paths) if snapshot else [],
        "scope_violations": list(snapshot.scope_violations) if snapshot else [],
        "protected_paths": list(snapshot.protected_paths) if snapshot else [],
        "protected_approval_event_id": snapshot.protected_approval_event_id if snapshot else None,
        "gates": report_gates,
    }
    try:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise GateExecutionUnavailable(f"gate report を保存できません: {exc}") from exc
    return report, int(report["exit_code"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m vsm.gates.runner",
        description="候補 worktree を信頼できる独立ゲートで検証します。",
    )
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--base", default=None, help="比較対象の Git ref (既定: HEAD)")
    parser.add_argument("--gates", default=",".join(DEFAULT_GATES))
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report, exit_code = run(
            args.worktree,
            base=args.base,
            gates=args.gates,
            out=args.out,
        )
    except (GateInputError, GateExecutionUnavailable) as exc:
        print(f"gate runner error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
