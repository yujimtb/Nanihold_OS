# EEP track integration notes

Track A exposes the following contract-layer connection points:

```python
from vsm.environment import (
    EnvironmentContractArtifactStore,
    environment_fingerprint,
)

artifact = store.get(artifact_key=artifact_key, version=artifact_version)
contract = artifact.contract
expected = environment_fingerprint(contract)
if declared_environment_fingerprint != expected:
    raise ConfigurationError("environment_fingerprint does not match the contract")
```

`EnvironmentContractArtifactStore` is the protocol implemented by
`LocalEnvironmentContractStore` for development and by
`LetheEnvironmentContractStore` over an injected transport. `save(contract,
artifact_key=..., version=...)` writes a versioned
`environment-contract` artifact; `get(artifact_key=..., version=...)` returns a
validated artifact and guarantees that its stored fingerprint matches the
deserialized contract.

The LETHE integration point for a control-plane implementation is
`VersionedArtifactTransport.put_versioned_artifact` /
`get_versioned_artifact`. A control-plane adapter must supply that transport
against the commissioned LETHE client. PilotHost must retrieve the artifact before environment
selection and pass only the validated `EnvironmentContract` and its
fingerprint into its instance/preflight flow.

## Integrated preflight and instance connections

`vsm.preflight.PreflightGate` accepts Track A's concrete
`vsm.environment.EnvironmentContract`. The contract has common capabilities and
an `adapters` map of `AdapterRequirement` values. It computes the adapter-aware
verification tuple with `environment_fingerprint(contract)`; a caller cannot
supply a second, potentially divergent contract fingerprint. The PilotHost JSON
parser validates the same formal contract and compares the computed value with
each declared adapter candidate before constructing the gate. The contract-wide
endpoint set is derived from the adapter map; the old single endpoint/version
fields are intentionally not retained.

Track C's `EnvironmentInstance.from_contract(contract, ...)` uses the same
function and keeps its machine-specific `instance_fingerprint` separate.
Successful preflight evidence connects to the lifecycle Ledger with:

```python
evidence_hook = instance_service.preflight_evidence_hook(
    instance_id,
    idempotency_key_prefix="environment:preflight:verify",
)
gate = PreflightGate(
    contract=contract,
    instance_fingerprint=instance.instance_fingerprint,
    evidence_hook=evidence_hook,
    # version_readers, cache_path, preflight_runners, ...
)
```

The hook rejects contract or instance fingerprint mismatches and records the
full `PreflightEvidence` in an `environment_instance_verified` Operational
Ledger Event. `DependencyAwareDispatcher` also accepts the lifecycle service
through its `EnvironmentFailover` protocol and invokes it when PilotHost is
unreachable, while retaining the ACR#3 `agent_naming_registry` injection.

## Phase 1 production wiring

`NaniholdConfig` now carries the commissioned local artifact selector and
`EnvironmentInstance` binding under `environment_contract_artifact` and
`environment_instance`. Runtime bootstrap retrieves the immutable artifact from
`LocalEnvironmentContractStore`, requires it to equal the Kernel's declared
`environment_contract`, computes the instance fingerprint from the concrete
bindings, and injects an active `EnvironmentInstanceService` into
`DependencyAwareDispatcher(environment_failover=...)`. The configured
`preflight_instance_fingerprint` must equal that computed binding fingerprint.

`ProductionPilotHost` accepts `preflight.kernel_config_path`. When present, the
Kernel TOML is authoritative for `preflight_enabled`, adapter別 version/cache paths,
contract, artifact selector, and instance binding; the PilotHost JSON `preflight`
fields are the explicit fallback used when that path is absent. A configured
local artifact is validated through `LocalEnvironmentContractStore` and must
match any inline contract. A configured instance replaces Codex's executable,
workspace, `CODEX_HOME`, and child environment with its explicit bindings. The
default runner adds endpoint, memory, shell, path, and workspace-write probes to
the Codex rollout observation. With an explicit Operational Ledger connection,
the launcher creates `EnvironmentInstanceService` and uses its
`preflight_evidence_hook(instance_id, idempotency_key_prefix=...)`.

The control-plane `VersionedArtifactTransport` adapter remains a separate
deployment integration. Phase 1's local store connection is intentionally
fail-fast and does not silently substitute an uncommissioned artifact.
- The built-in Codex trial reads rollout sandbox policy and workspace mode; when
  explicit EnvironmentInstance bindings are present, the adapter-specific runner adds
  endpoint, memory, shell, logical-path, and workspace-write measurements.
- Claude Code and Codex CLI have separate version readers and preflight runners;
  dispatching an adapter absent from `EnvironmentContract.adapters` fails closed.
- The environment lifecycle records registration, verification, activation,
  retirement, failover, and reprovision requests. A procurement-boundary-aware
  provisioner and asynchronous owner notification transport remain to be
  connected for real discovery/rebuild operations.
