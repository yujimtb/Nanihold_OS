from __future__ import annotations

import json
import re
from pathlib import Path


HA = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (HA / relative).read_text(encoding="utf-8")


def test_json_contracts_are_strict_and_portable() -> None:
    for name in (
        "contracts/deployment-input.schema.json",
        "contracts/secret-input.schema.json",
        "contracts/preflight-receipt.schema.json",
    ):
        schema = json.loads(read(name))
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "(?i)" not in json.dumps(schema)

    secret = json.loads(read("contracts/secret-input.schema.json"))
    secret_rule = secret["$defs"]["secret"]
    assert secret_rule["minLength"] >= 32
    assert "not" in secret_rule


def test_topology_has_two_separate_three_node_clusters() -> None:
    topology = read("topology.psd1")
    assert "SchemaVersion = 2" in topology
    assert "ExistingMcpGateway = '172.31.100.10'" in topology
    assert "Vip = '172.31.100.20'" in topology
    assert "Vip = '172.31.100.30'" in topology
    assert "RpoSeconds = 0" in topology
    assert "RtoSeconds = 300" in topology
    assert topology.count("Cluster = 'nanihold'") == 3
    assert topology.count("Cluster = 'lethe'") == 3
    addresses = re.findall(r"Address = '(\d+\.\d+\.\d+\.\d+)'", topology)
    assert addresses == [
        "172.31.100.21",
        "172.31.100.22",
        "172.31.100.23",
        "172.31.100.31",
        "172.31.100.32",
        "172.31.100.33",
    ]
    assert "PodCidr = '10.50.0.0/16'" in topology
    assert "PodCidr = '10.60.0.0/16'" in topology
    assert "ServiceCidr = '10.51.0.0/16'" in topology
    assert "ServiceCidr = '10.61.0.0/16'" in topology


def test_all_operational_inputs_are_explicit_and_fail_fast() -> None:
    scripts = {
        path.name: path.read_text(encoding="utf-8")
        for path in HA.glob("*.ps1")
    }
    for name in (
        "preflight.ps1",
        "bootstrap.ps1",
        "verify.ps1",
        "backup.ps1",
        "restore.ps1",
        "measure-failover.ps1",
    ):
        body = scripts[name]
        assert "[string] $DeploymentInputPath" in body
        assert "[string] $SecretInputPath" in body
        assert "[Parameter(Mandatory)]" in body
    assert "Assert-NasWritable" in scripts["preflight.ps1"]
    assert "Assert-NasWritable" in scripts["provision.ps1"]
    assert "Assert-NasWritable" in scripts["bootstrap.ps1"]
    assert "Assert-NasWritable" in scripts["verify.ps1"]
    assert "Assert-NasWritable" in scripts["backup.ps1"]
    assert "Assert-NasWritable" in scripts["restore.ps1"]
    module = read("HaContract.psm1")
    assert "backup_image_archive_path" in module
    assert "backup_image_archive_sha256" in module
    assert "Backup image archive" in module
    assert "SilentlyContinue" not in read("HaContract.psm1")
    assert "os.getenv" not in "\n".join(scripts.values())


def test_lethe_storage_is_synchronous_and_has_no_runtime_fallback() -> None:
    lethe = read("manifests/lethe.yaml.tmpl")
    assert 'numberOfReplicas: "3"' in lethe
    assert "instances: 3" in lethe
    assert "minSyncReplicas: 1" in lethe
    assert "maxSyncReplicas: 1" in lethe
    assert 'synchronous_commit: "remote_apply"' in lethe
    assert "name: lethe-event-api" in lethe
    assert "name: lethe-blob" in lethe
    assert "name: lethe-projection" in lethe
    assert "externalTrafficPolicy: Cluster" in lethe
    lowered = lethe.lower()
    assert "sqlite" not in lowered
    assert "fallback" not in lowered
    assert "double-write" not in lowered


def test_nanihold_has_two_pinned_pilot_hosts_and_typed_config() -> None:
    manifest = read("manifests/nanihold.yaml.tmpl")
    renderer = read("render-manifests.ps1")
    assert "replicas: 2" in manifest
    assert "name: pilot-host-a" in manifest
    assert "name: pilot-host-b" in manifest
    assert "kubernetes.io/hostname: nh-control-a" in manifest
    assert "kubernetes.io/hostname: nh-control-b" in manifest
    assert "externalTrafficPolicy: Cluster" in manifest
    assert "model_snapshot = 'claude-fable-5'" in renderer
    assert "effort = 'high'" in renderer
    assert "$deployment.mcp_allowlist" in renderer
    assert "mcp__history__*" in renderer
    assert "mcp__gateway__*" in renderer
    assert "PILOT_HOST_BEARER_TOKEN" in renderer
    assert "ANTHROPIC_API_KEY" not in renderer
    assert "OPENAI_API_KEY" not in renderer


def test_secret_values_are_only_sent_as_kubernetes_secret_stdin() -> None:
    bootstrap = read("bootstrap.ps1")
    assert "-StandardInput $secret -SecretInput" in bootstrap
    assert "New-KubernetesSecretJson" in bootstrap
    assert "SecretValuesLogged = $false" in bootstrap
    assert "k3s ctr images import /tmp/backup-image.tar" in bootstrap
    assert "k3s ctr images list -q" in bootstrap
    assert "$deployment.backup_image_archive_sha256" in bootstrap
    verify = read("verify.ps1")
    assert "Assert-BackupImageContract" in verify
    assert "@('contract', '--format', 'json')" in verify
    assert "'backup,restore'" in verify
    assert "canonical-event-blob-projection-postgres" in verify
    manifests = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (HA / "manifests").glob("*")
    )
    assert "value: sk-" not in manifests
    assert "api_key:" not in manifests.lower()
    assert "bearer_token:" not in manifests.lower()


def test_backup_restore_covers_all_canonical_state() -> None:
    template = read("manifests/backup.yaml.tmpl")
    backup = read("backup.ps1")
    empty = read("empty-target.ps1")
    restore = read("restore.ps1")
    assert "canonical-event-blob-projection-postgres" in template
    for field in (
        "event_export_sha256",
        "blob_manifest_sha256",
        "projection_cursor",
        "postgres_backup_sha256",
        "signature_sha256",
    ):
        assert field in backup
    assert "$manifest.projection_cursor -ne $manifest.event_cursor" in backup
    assert "/api/restore/state" in empty
    assert "$state.event_count -ne 0" in empty
    assert "$state.blob_count -ne 0" in empty
    assert "$state.projection_count -ne 0" in empty
    assert "RESTORE_ONLY_INTO_VERIFIED_EMPTY_LETHE" in restore
    assert "--require-empty-target" in restore
    assert "canonical-event-blob-projection-postgres" in restore
    assert "Backup manifest is outside the explicit NAS root." in restore
    assert '"https://$($lethe.Vip)/api/restore/state"' in restore
    assert "https://172.31.100.30/api/restore/state" not in restore


def test_failover_measurement_proves_rpo_and_rto_without_model_calls() -> None:
    measurement = read("measure-failover.ps1")
    plan_index = measurement.index("if ($Mode -ceq 'Plan')")
    canary_write_index = measurement.index("-Uri \"https://$($lethe.Vip)/api/ha/canaries\"")
    assert plan_index < canary_write_index
    assert "INTERRUPT_ONE_VERIFIED_HA_MEMBER" in measurement
    assert "Event, blob, and Projection" in measurement
    assert "$after.event_cursor -ne $before.event_cursor" in measurement
    assert "rpo_seconds = 0" in measurement
    assert "RtoSeconds * 1000" in measurement
    assert "$consecutiveReady -eq 3" in measurement
    assert "Start-Sleep -Milliseconds 1000" in measurement
    lowered = measurement.lower()
    assert "claude" not in lowered
    assert "opus" not in lowered
    assert "codex" not in lowered
    assert "model" not in lowered


def test_monitoring_covers_the_availability_boundary() -> None:
    rules = read("manifests/monitoring-rules.yaml")
    for alert in (
        "NaniholdVipUnavailable",
        "LetheVipUnavailable",
        "LethePostgresNotSynchronous",
        "LetheProjectionBehind",
        "PilotHostTransportUnknown",
        "NasBackupStale",
    ):
        assert f"alert: {alert}" in rules


def test_documentation_discloses_current_blockers_and_ha_boundary() -> None:
    docs = read("README.md")
    for text in (
        "管理者sessionではない",
        "NAS backup targetが未指定・未到達",
        "Ubuntu Server ISO",
        "SSH公開鍵、秘密鍵",
        "VM作成、network変更、NAS書込、k3s導入は実行していません",
        "RPO 0",
        "RTO 300秒",
        "物理PC",
        "POST /api/ha/canaries",
        "GET /api/ha/canaries/{canary_id}",
        "/api/restore/state",
    ):
        assert text in docs


def test_runtime_capability_receipt_is_validated_not_only_hashed() -> None:
    contract = read("HaContract.psm1")

    assert "$null = Read-RuntimeContractReceipt -Deployment $input" in contract
    assert "RUNTIME_CONTRACT_UNAVAILABLE" in contract
    assert "implemented-verified" in contract


def test_no_removed_run_or_chat_api_is_reintroduced() -> None:
    implementation = "\n".join(
        path.read_text(encoding="utf-8")
        for path in HA.rglob("*")
        if path.is_file() and path.suffix in {".ps1", ".psm1", ".yaml", ".tmpl"}
    )
    assert "/api/runs" not in implementation
    assert "/api/chat" not in implementation
    assert "native_runs_enabled" not in implementation
