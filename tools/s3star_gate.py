from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def tracked_files() -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return tuple(line for line in completed.stdout.splitlines() if line)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"S3* BLOCKING: {message}")


def main() -> None:
    tracked = tracked_files()
    forbidden_prefixes = (
        "bot/",
        "openspec/changes/",
        "vsm/agents/",
        "vsm/eventlog/",
        "vsm/runtime/",
        "vsm/selfdev/",
        "vsm/survival/",
        "vsm/systems/",
        "vsm/tools/",
    )
    for path in tracked:
        require(
            not path.startswith(forbidden_prefixes),
            f"removed runtime surface remains tracked: {path}",
        )
        require(not path.lower().endswith(".pdf"), f"PDF must remain untracked: {path}")

    runtime_text = "\n".join(
        path.read_text("utf-8")
        for path in sorted((ROOT / "vsm").rglob("*.py"))
        if "migration" not in path.parts
    )
    for token in (
        "RunConfig",
        "run_id",
        "NodeRunState",
        "native_runs_enabled",
        '"/api/runs',
        '"/api/chat',
        "events.jsonl",
    ):
        require(token not in runtime_text, f"legacy runtime token remains: {token}")

    web = (ROOT / "vsm" / "web" / "app.py").read_text("utf-8")
    for endpoint in (
        "/api/data-spaces",
        "/api/nodes",
        "/api/work-items",
        "/api/executions",
        "/api/events",
        "/api/conversations",
        "/api/pilot-hosts",
        "/api/model-registry",
        "/api/route-snapshots",
        "/api/token-lab",
        "/api/history/imports",
        "/api/history/sessions",
        "/api/reorientation",
        "/api/reorientation/approval",
        "/api/activation/status",
        "/api/conversations/{conversation_id}/actions",
        "/api/work-items/{work_item_id}/delegations",
        "/api/effects/{lease_id}/approval",
    ):
        require(endpoint in web, f"required API endpoint missing: {endpoint}")

    config = (ROOT / "vsm" / "config.py").read_text("utf-8")
    require(
        'interface.model_snapshot != "claude-fable-5"' in config,
        "production Interface Pilot model is not fixed to claude-fable-5",
    )
    require(
        'interface.effort != "high"' in config,
        "production Interface Pilot effort is not fixed to high",
    )

    activation = (ROOT / "vsm" / "activation" / "models.py").read_text("utf-8")
    for state in (
        "UNCOMMISSIONED",
        "HISTORY_IMPORTED",
        "REORIENTATION_ONLY",
        "AWAITING_OWNER_CONFIRMATION",
        "ACTIVE",
    ):
        require(state in activation, f"activation state missing: {state}")

    require(
        "/api/conversations/{conversation_id}/messages" not in web,
        "removed conversation messages endpoint remains",
    )

    token_lab = (ROOT / "vsm" / "token_lab" / "lab.py").read_text("utf-8")
    require(
        '"fable" in lowered or "opus" in lowered' in token_lab,
        "Token Efficiency Lab does not reject Fable and Opus",
    )

    golden = ROOT / "tests" / "fixtures" / "fable-owner-ux-golden-119.json"
    require(golden.is_file(), "119-case UX golden manifest is missing")
    golden_text = golden.read_text("utf-8")
    require(
        '"case_count": 119' in golden_text,
        "UX golden case count is not 119",
    )
    require(
        "Raw owner text is intentionally excluded" in golden_text,
        "UX golden privacy declaration is missing",
    )
    print("S3* PASS: destructive cutover, UX, routing, and privacy invariants")


if __name__ == "__main__":
    main()
