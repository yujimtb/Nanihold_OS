"""EEP contract layer public API.

Track B can integrate PilotHost startup with
``EnvironmentContractArtifactStore.get(...).contract`` and verify the declared
candidate value using ``environment_fingerprint(contract)``.  This package
does not define or bind an EnvironmentInstance.
"""

from .approval import (
    ApprovalTargetKind,
    OwnerApprovalRequest,
    OwnerApprovalTarget,
    ProcurementPolicyBoundary,
)
from .artifacts import (
    EnvironmentContractArtifact,
    EnvironmentContractArtifactStore,
    EnvironmentContractArtifactType,
    LetheEnvironmentContractStore,
    LocalEnvironmentContractStore,
    VersionedArtifactTransport,
    deserialize_environment_contract_artifact,
    serialize_environment_contract_artifact,
)
from .contracts import (
    AdapterRequirement,
    ENVIRONMENT_FINGERPRINT_PREFIX,
    EnvironmentContract,
    EnvironmentModel,
    SandboxMode,
    environment_fingerprint,
)

__all__ = [
    "ApprovalTargetKind",
    "AdapterRequirement",
    "ENVIRONMENT_FINGERPRINT_PREFIX",
    "EnvironmentContract",
    "EnvironmentContractArtifact",
    "EnvironmentContractArtifactStore",
    "EnvironmentContractArtifactType",
    "EnvironmentModel",
    "LetheEnvironmentContractStore",
    "LocalEnvironmentContractStore",
    "OwnerApprovalRequest",
    "OwnerApprovalTarget",
    "ProcurementPolicyBoundary",
    "SandboxMode",
    "VersionedArtifactTransport",
    "deserialize_environment_contract_artifact",
    "environment_fingerprint",
    "serialize_environment_contract_artifact",
]
