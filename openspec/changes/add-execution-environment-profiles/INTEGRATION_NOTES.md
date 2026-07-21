# Track A / Track B integration notes

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
`LocalEnvironmentContractStore` for development and by the Track B LETHE
adapter. `save(contract, artifact_key=..., version=...)` writes a versioned
`environment-contract` artifact; `get(artifact_key=..., version=...)` returns a
validated artifact and guarantees that its stored fingerprint matches the
deserialized contract.

The LETHE integration point for a control-plane implementation is
`VersionedArtifactTransport.put_versioned_artifact` /
`get_versioned_artifact`. Track B supplies that transport against the existing
LETHE client. PilotHost should retrieve the artifact before environment
selection and pass only the validated `EnvironmentContract` and its
fingerprint into its instance/preflight flow.

The contract layer deliberately defines no `EnvironmentInstance`, physical
path, CLI executable path, `CODEX_HOME`, or PilotHost execution logic. The
files `scripts/production_pilot_host.py` and `vsm/pilot/production_host.py`
remain unchanged for Track B.
