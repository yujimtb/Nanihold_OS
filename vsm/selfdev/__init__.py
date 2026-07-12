"""自己開発ループ Wave 1 のドメイン層。"""

from vsm.selfdev.models import (
    AcceptanceCriterion,
    ActorRef,
    AuditReport,
    BudgetEstimate,
    GateReport,
    PathRule,
    ProposalManifest,
    ProposalOrigin,
    PRDescription,
    RunRuntime,
    ScopeCheck,
    is_protected_path,
    proposal_to_run_manifest,
)
from vsm.selfdev.state_machine import (
    PauseCause,
    PauseKind,
    ProposalAggregate,
    ProposalPhase,
    ProposalStateMachine,
    assert_transition_allowed,
)
from vsm.selfdev.git import CandidateCommit, CandidateCommitter
from vsm.selfdev.verification import (
    ProtectedApproval,
    REQUIRED_GATES,
    ScopeCheckResult,
    canonical_scope,
    scope_sha256,
    verify_protected_approval,
    verify_scope,
)
from vsm.selfdev.workspace import ProposalWorkspace, WorkspaceController, WorkspaceDescriptor, WorkspaceStatus

__all__ = [
    "AcceptanceCriterion",
    "ActorRef",
    "AuditReport",
    "BudgetEstimate",
    "GateReport",
    "PathRule",
    "ProposalAggregate",
    "ProposalManifest",
    "ProposalOrigin",
    "ProposalPhase",
    "ProposalStateMachine",
    "PRDescription",
    "PauseCause",
    "PauseKind",
    "RunRuntime",
    "ScopeCheck",
    "assert_transition_allowed",
    "is_protected_path",
    "proposal_to_run_manifest",
    "CandidateCommit",
    "CandidateCommitter",
    "ProtectedApproval",
    "REQUIRED_GATES",
    "ScopeCheckResult",
    "canonical_scope",
    "scope_sha256",
    "verify_protected_approval",
    "verify_scope",
    "ProposalWorkspace",
    "WorkspaceController",
    "WorkspaceDescriptor",
    "WorkspaceStatus",
    "AuditError",
    "AuditRunner",
    "S3StarAuditRunner",
    "ConsortiumAdapterError",
    "DurableHumanWaiter",
    "HumanTimeout",
    "HumanTimeoutPolicy",
    "SelfDevConsortiumAdapter",
    "ControllerError",
    "ControllerPaused",
    "ImplementationResult",
    "QuotaWait",
    "SelfDevController",
    "ReadyQueueScheduler",
    "SchedulerDecision",
    "SelfDevScheduler",
    "SelfDevService",
]


def __getattr__(name: str):
    """Wave 3 modules are loaded lazily to keep Event schema bootstrap acyclic."""

    if name in {"AuditError", "AuditRunner", "S3StarAuditRunner"}:
        from vsm.selfdev.audit import AuditError, AuditRunner, S3StarAuditRunner

        return {"AuditError": AuditError, "AuditRunner": AuditRunner, "S3StarAuditRunner": S3StarAuditRunner}[name]
    if name in {
        "ConsortiumAdapterError",
        "DurableHumanWaiter",
        "HumanTimeout",
        "HumanTimeoutPolicy",
        "SelfDevConsortiumAdapter",
    }:
        from vsm.selfdev.consortium_adapter import (
            ConsortiumAdapterError,
            DurableHumanWaiter,
            HumanTimeout,
            HumanTimeoutPolicy,
            SelfDevConsortiumAdapter,
        )

        return {
            "ConsortiumAdapterError": ConsortiumAdapterError,
            "DurableHumanWaiter": DurableHumanWaiter,
            "HumanTimeout": HumanTimeout,
            "HumanTimeoutPolicy": HumanTimeoutPolicy,
            "SelfDevConsortiumAdapter": SelfDevConsortiumAdapter,
        }[name]
    if name in {"ControllerError", "ControllerPaused", "ImplementationResult", "QuotaWait", "SelfDevController"}:
        from vsm.selfdev.controller import ControllerError, ControllerPaused, ImplementationResult, QuotaWait, SelfDevController

        return {
            "ControllerError": ControllerError,
            "ControllerPaused": ControllerPaused,
            "ImplementationResult": ImplementationResult,
            "QuotaWait": QuotaWait,
            "SelfDevController": SelfDevController,
        }[name]
    if name in {"ReadyQueueScheduler", "SchedulerDecision", "SelfDevScheduler"}:
        from vsm.selfdev.scheduler import ReadyQueueScheduler, SchedulerDecision, SelfDevScheduler

        return {"ReadyQueueScheduler": ReadyQueueScheduler, "SchedulerDecision": SchedulerDecision, "SelfDevScheduler": SelfDevScheduler}[name]
    if name == "SelfDevService":
        from vsm.selfdev.service import SelfDevService

        return SelfDevService
    raise AttributeError(name)
