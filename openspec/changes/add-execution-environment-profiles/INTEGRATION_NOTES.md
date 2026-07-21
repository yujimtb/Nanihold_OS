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
`vsm.environment.EnvironmentContract`. It computes the verification tuple with
`environment_fingerprint(contract)`; a caller cannot supply a second,
potentially divergent contract fingerprint. The PilotHost JSON parser validates
the same formal contract and compares the computed value with the Codex
candidate declaration before constructing the gate.

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
    # version_reader, cache_path, preflight_runner, ...
)
```

The hook rejects contract or instance fingerprint mismatches and records the
full `PreflightEvidence` in an `environment_instance_verified` Operational
Ledger Event. `DependencyAwareDispatcher` also accepts the lifecycle service
through its `EnvironmentFailover` protocol and invokes it when PilotHost is
unreachable, while retaining the ACR#3 `agent_naming_registry` injection.

## Remaining production wiring TODOs

- `scripts/production_pilot_host.py` has an explicit `TODO(EEP production
  wiring)`: the production JSON does not yet identify a commissioned DataSpace,
  EnvironmentInstance ID, or Operational Ledger connection, so the launcher
  cannot construct and inject the lifecycle evidence hook itself.
- `vsm/runtime.py` has the corresponding failover TODO: active instance
  identity and concrete bindings are not yet in `NaniholdConfig`, so runtime
  cannot construct `EnvironmentInstanceService` and inject it into the
  dispatcher without inventing configuration.
- PilotHost startup does not yet retrieve the selected versioned contract from
  the control plane. The `VersionedArtifactTransport` implementation and
  artifact key/version settings must be added without a local fallback.
- The built-in Codex trial reads rollout sandbox policy and workspace mode.
  Endpoint, memory, shell, and logical-path probes require an instance-aware
  runner built from explicit EnvironmentInstance bindings; until it is wired,
  the gate fails closed when those required measurements are absent.
- The environment lifecycle records registration, verification, activation,
  retirement, failover, and reprovision requests. A procurement-boundary-aware
  provisioner and asynchronous owner notification transport remain to be
  connected for real discovery/rebuild operations.
