"""本番 FastAPI から selfdev controller を組み立てる配線。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from vsm.config import LLMConfig, RunConfig, load_config
from vsm.errors import ConfigError
from vsm.gates.runner import run_v2
from vsm.runtime.lifecycle import _resolve_role_runtimes
from vsm.roles import SystemRole
from vsm.selfdev.audit import S3StarAuditRunner
from vsm.selfdev.consortium_adapter import HumanTimeoutPolicy
from vsm.selfdev.controller import SelfDevController, _RuntimeImplementationRunner
from vsm.selfdev.models import GateReport, RunRuntime
from vsm.selfdev.service import SelfDevService
from vsm.selfdev.store import SelfDevEventStore


class _TrustedGateWorker:
    """controller から control-plane の GateRunner v2 だけを呼ぶ adapter。"""

    async def run(self, *, manifest: Any, worktree: Path, output_dir: Path) -> GateReport:
        report, _exit_code = await asyncio.to_thread(
            run_v2,
            worktree,
            base=manifest.base_sha,
            out=output_dir,
            proposal_id=manifest.proposal_id,
            implementation_run_id=manifest.run_id,
            gate_attempt=manifest.attempt,
            scope=manifest.scope,
            scope_sha256=manifest.scope_sha256,
            risk_class=manifest.risk_class,
            proposal_manifest_sha256=manifest.proposal_manifest_sha256,
            protected_scope_sha256=manifest.scope_sha256,
            protected_approval=None,
            protected_approval_event_id=manifest.protected_approval_event_id,
        )
        return GateReport.model_validate(report)


def _runtime_binding(role: SystemRole, runtime: Any, run_config: Any) -> RunRuntime:
    backend_name = run_config.agents.backend_for(role)
    if backend_name is None:
        raise ConfigError(missing_roles=[role.value], detail="selfdev runtime が未設定です")
    backend = run_config.agents.backends[backend_name]
    model = backend.model or str(getattr(runtime, "model", ""))
    effort = backend.reasoning_effort or "standard"
    if not model:
        raise ConfigError(missing_roles=[role.value], detail="selfdev runtime model が未設定です")
    return RunRuntime(role=role.value, backend=backend_name, model=model, reasoning_effort=effort)


def build_selfdev_service(
    *,
    config: tuple[LLMConfig, RunConfig] | None = None,
    process_factory: Callable[..., Any] | None = None,
) -> SelfDevService | None:
    """``[selfdev].enabled`` が明示された場合だけ本番 service を作る。"""

    _llm_config, run_config = config if config is not None else load_config(None)
    if not run_config.selfdev.enabled:
        return None
    runtimes = _resolve_role_runtimes(
        run_config=run_config,
        llm_config=_llm_config,
        llm_override=None,
        runtime_overrides=None,
        process_factory=process_factory,
    )
    required = (SystemRole.S1_WORKER, SystemRole.S3_ALLOCATOR, SystemRole.S4_SCANNER, SystemRole.S5_POLICY, SystemRole.S3STAR_AUDITOR)
    missing = [role.value for role in required if runtimes.get(role) is None]
    if missing:
        raise ConfigError(missing_roles=missing, detail="selfdev の required runtime が未設定です")
    store_root = Path("runs") / "selfdev"
    store = SelfDevEventStore(store_root)
    timeout = run_config.consortium.human_timeout_seconds
    controller = SelfDevController(
        repository=run_config.selfdev.repository,
        store=store,
        writer_runtime=_runtime_binding(SystemRole.S1_WORKER, runtimes[SystemRole.S1_WORKER], run_config),
        implementation_runner=_RuntimeImplementationRunner(runtimes[SystemRole.S1_WORKER]),
        gate_runner=_TrustedGateWorker(),
        audit_runner=S3StarAuditRunner(runtime=runtimes[SystemRole.S3STAR_AUDITOR]),
        consortium_runtimes={
            SystemRole.S3_ALLOCATOR: runtimes[SystemRole.S3_ALLOCATOR],
            SystemRole.S4_SCANNER: runtimes[SystemRole.S4_SCANNER],
            SystemRole.S5_POLICY: runtimes[SystemRole.S5_POLICY],
        },
        timeout_policy=HumanTimeoutPolicy(
            timeout_seconds={"low": timeout, "normal": timeout, "protected": timeout},
            timeout_action={"low": "proceed", "normal": "abort", "protected": "abort"},
        ),
        worktree_root=store_root / "worktrees",
        base_ref="main",
        clock=None,
        implementation_timeout_margin_seconds=(
            run_config.selfdev.implementation_timeout_margin_seconds
        ),
    )
    return SelfDevService(controller)


__all__ = ["build_selfdev_service"]
