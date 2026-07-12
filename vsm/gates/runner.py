"""Trusted GateRunner command line and gate orchestration."""

from __future__ import annotations

import argparse
import json
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


def _run_command(command: Sequence[str], *, cwd: Path, log_path: Path) -> int:
    _write_log(log_path, "$ " + _command_text(command))
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
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


def _run_g2(worktree: Path, log_path: Path) -> tuple[str, str, tuple[str, ...]]:
    returncode = _run_command(G2_COMMAND, cwd=worktree, log_path=log_path)
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
        lint_code = _run_command(G3_LINT_COMMAND, cwd=frontend_dir, log_path=log_path)
        if lint_code != 0:
            findings.append(f"lint exit code {lint_code}")
    build_code = _run_command(G3_BUILD_COMMAND, cwd=frontend_dir, log_path=log_path)
    if build_code != 0:
        findings.append(f"build exit code {build_code}")
    if findings:
        return "fail", "frontend gate failed: " + ", ".join(findings), tuple(findings)
    commands = ["npm run lint"] if "lint" in scripts else []
    commands.append("npm run build")
    return "pass", "frontend gate が成功しました", tuple(commands)


def _run_g4(worktree: Path, log_path: Path) -> tuple[str, str, tuple[str, ...]]:
    help_code = _run_command(G4_HELP_COMMAND, cwd=worktree, log_path=log_path)
    if help_code != 0:
        return "fail", f"python -m vsm --help が失敗しました (exit={help_code})", (
            f"help exit code {help_code}",
        )
    smoke_code = _run_command(G4_SMOKE_COMMAND, cwd=worktree, log_path=log_path)
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
) -> tuple[dict[str, object], int]:
    """Run the selected trusted gates and write ``gate_report.json``."""

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
