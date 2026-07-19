Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-WindowsPlatform {
    return [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
        [Runtime.InteropServices.OSPlatform]::Windows
    )
}

function Assert-ExactKeys {
    param(
        [Parameter(Mandatory)][object] $Value,
        [Parameter(Mandatory)][string[]] $Keys,
        [Parameter(Mandatory)][string] $Label
    )

    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $expected = @($Keys | Sort-Object)
    if (($actual -join "`n") -cne ($expected -join "`n")) {
        throw "$Label fields differ from the exact contract."
    }
}

function Assert-Administrator {
    if (-not (Test-WindowsPlatform)) {
        throw 'Hyper-V operations require Windows PowerShell.'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Hyper-V operation requires an elevated PowerShell session.'
    }
}

function Assert-File {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Label
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not found: $Path"
    }
}

function Get-FileSha256 {
    param([Parameter(Mandatory)][string] $Path)
    Assert-File -Path $Path -Label 'Digest input'
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-FileDigest {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Expected,
        [Parameter(Mandatory)][string] $Label
    )
    if ($Expected -cnotmatch '^[0-9a-f]{64}$') {
        throw "$Label expected digest is not lowercase SHA-256."
    }
    $actual = Get-FileSha256 -Path $Path
    if ($actual -cne $Expected) {
        throw "$Label digest mismatch."
    }
}

function Read-ExactJson {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string[]] $Keys,
        [Parameter(Mandatory)][string] $Label
    )
    Assert-File -Path $Path -Label $Label
    try {
        $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
            ConvertFrom-Json -Depth 64
    }
    catch {
        throw "$Label is not valid JSON."
    }
    Assert-ExactKeys -Value $value -Keys $Keys -Label $Label
    return $value
}

function Assert-NonBlank {
    param(
        [Parameter(Mandatory)][object] $Value,
        [Parameter(Mandatory)][string] $Label
    )
    if ($Value -isnot [string] -or [string]::IsNullOrWhiteSpace($Value)) {
        throw "$Label must be a non-blank string."
    }
}

function Read-DeploymentInput {
    param([Parameter(Mandatory)][string] $Path)
    $keys = @(
        'schema_version',
        'ubuntu_iso_sha256',
        'k3s_version',
        'k3s_install_script_path',
        'k3s_install_script_sha256',
        'k3s_binary_path',
        'k3s_binary_sha256',
        'oscdimg_path',
        'oscdimg_sha256',
        'kube_vip_image',
        'longhorn_manifest_path',
        'longhorn_manifest_sha256',
        'cloudnative_pg_manifest_path',
        'cloudnative_pg_manifest_sha256',
        'monitoring_manifest_path',
        'monitoring_manifest_sha256',
        'nanihold_image',
        'lethe_image',
        'pilot_host_image',
        'backup_image',
        'backup_image_archive_path',
        'backup_image_archive_sha256',
        'runtime_contract_receipt_path',
        'runtime_contract_receipt_sha256',
        'claude_cli_version',
        'codex_cli_version',
        'codex_model',
        'codex_effort',
        'pilot_permission_mode',
        'pilot_sandbox_certificate_sha256',
        'claude_max_budget_usd',
        'pilot_timeout_seconds',
        'codex_max_input_tokens',
        'codex_max_output_tokens',
        'codex_max_total_tokens',
        'mcp_gateway_url',
        'mcp_allowlist',
        'pilot_hosts',
        'nanihold_runtime',
        'route_priors',
        'lethe_data_space_id',
        'postgres_storage_gib',
        'blob_storage_gib',
        'nas_nfs_server',
        'nas_nfs_export'
    )
    $input = Read-ExactJson -Path $Path -Keys $keys -Label 'Deployment input'
    if ($input.schema_version -ne 1) {
        throw 'Unsupported deployment input schema.'
    }
    foreach ($name in @(
        'ubuntu_iso_sha256',
        'k3s_install_script_sha256',
        'k3s_binary_sha256',
        'oscdimg_sha256',
        'longhorn_manifest_sha256',
        'cloudnative_pg_manifest_sha256',
        'monitoring_manifest_sha256',
        'backup_image_archive_sha256',
        'runtime_contract_receipt_sha256'
    )) {
        if ($input.$name -isnot [string] -or $input.$name -cnotmatch '^[0-9a-f]{64}$') {
            throw "Deployment input $name must be lowercase SHA-256."
        }
    }
    foreach ($name in @(
        'kube_vip_image',
        'nanihold_image',
        'lethe_image',
        'pilot_host_image',
        'backup_image'
    )) {
        if ($input.$name -isnot [string] -or
            $input.$name -cnotmatch '^[^@\s]+@sha256:[0-9a-f]{64}$') {
            throw "Deployment image $name must be pinned by SHA-256 digest."
        }
    }
    foreach ($name in @(
        'k3s_version',
        'k3s_install_script_path',
        'k3s_binary_path',
        'oscdimg_path',
        'longhorn_manifest_path',
        'cloudnative_pg_manifest_path',
        'monitoring_manifest_path',
        'backup_image_archive_path',
        'runtime_contract_receipt_path',
        'claude_cli_version',
        'codex_cli_version',
        'codex_model',
        'codex_effort',
        'pilot_permission_mode',
        'mcp_gateway_url',
        'lethe_data_space_id',
        'nas_nfs_server',
        'nas_nfs_export'
    )) {
        Assert-NonBlank -Value $input.$name -Label "Deployment input $name"
    }
    if ($input.mcp_gateway_url -cnotmatch '^https://172\.31\.100\.10(?::[0-9]{1,5})?/') {
        throw 'MCP Gateway URL must use the existing 172.31.100.10 endpoint.'
    }
    if ($input.k3s_version -cnotmatch '^v[0-9][A-Za-z0-9.+-]{1,63}$') {
        throw 'k3s version must be an explicit release identifier.'
    }
    $tools = @($input.mcp_allowlist)
    if ($tools.Count -eq 0 -or ($tools | Sort-Object -Unique).Count -ne $tools.Count) {
        throw 'MCP allowlist must be non-empty and unique.'
    }
    foreach ($tool in $tools) {
        if ($tool -isnot [string] -or
            $tool -cnotmatch '^mcp__[a-z][a-z0-9_-]{0,62}__[A-Za-z0-9_.-]+$') {
            throw 'MCP allowlist contains a non-canonical typed tool.'
        }
    }
    if (-not ($tools | Where-Object { $_ -clike 'mcp__history__*' }) -or
        -not ($tools | Where-Object { $_ -clike 'mcp__gateway__*' })) {
        throw 'MCP allowlist requires explicit history and gateway tools.'
    }
    $pilotHosts = @($input.pilot_hosts)
    if ($pilotHosts.Count -ne 2) {
        throw 'Deployment input requires exactly two PilotHost identities.'
    }
    $pilotIds = @()
    $deviceIds = @()
    $pilotNodes = @()
    foreach ($pilot in $pilotHosts) {
        Assert-ExactKeys -Value $pilot `
            -Keys @(
                'pilot_host_id',
                'device_id',
                'device_certificate_sha256',
                'node_name'
            ) -Label 'PilotHost identity'
        Assert-NonBlank -Value $pilot.pilot_host_id -Label 'PilotHost ID'
        Assert-NonBlank -Value $pilot.device_id -Label 'PilotHost device ID'
        if ($pilot.device_certificate_sha256 -isnot [string] -or
            $pilot.device_certificate_sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw 'PilotHost device certificate must be lowercase SHA-256.'
        }
        if ($pilot.node_name -cnotin @('nh-control-a', 'nh-control-b')) {
            throw 'PilotHost must be pinned to a Nanihold control node.'
        }
        $pilotIds += $pilot.pilot_host_id
        $deviceIds += $pilot.device_id
        $pilotNodes += $pilot.node_name
    }
    if (($pilotIds | Sort-Object -Unique).Count -ne 1 -or
        ($deviceIds | Sort-Object -Unique).Count -ne 1 -or
        (@($pilotHosts.device_certificate_sha256 | Sort-Object -Unique)).Count -ne 1 -or
        ($pilotNodes | Sort-Object -Unique).Count -ne 2) {
        throw (
            'PilotHost active/standby entries must share one exact device identity ' +
            'and use two unique control nodes.'
        )
    }
    Assert-ExactKeys -Value $input.nanihold_runtime `
        -Keys @(
            'owner_id',
            'audit_policy_id',
            'control_policy_id',
            'interface_node_id',
            'interface_pilot_id',
            'coding_pilot_id',
            'active_route_snapshot_id',
            'server_allowed_origins',
            'authorized_device_ids',
            'owner_session_lifetime_seconds',
            'retention_days',
            'max_parallelism',
            'reorientation_max_tool_rounds',
            'sandbox_profile_id',
            'sandbox_certificate_file',
            'sandbox_write_roots',
            'sandbox_network_destinations',
            'sandbox_issued_at',
            'sandbox_expires_at'
        ) -Label 'Nanihold runtime'
    foreach ($name in @(
        'owner_id',
        'audit_policy_id',
        'control_policy_id',
        'interface_node_id',
        'interface_pilot_id',
        'coding_pilot_id',
        'active_route_snapshot_id',
        'sandbox_profile_id',
        'sandbox_certificate_file'
    )) {
        Assert-NonBlank -Value $input.nanihold_runtime.$name `
            -Label "Nanihold runtime $name"
        if ($input.nanihold_runtime.$name -match '(?i)EXAMPLE|REPLACE|PLACEHOLDER') {
            throw "Nanihold runtime $name contains a placeholder."
        }
    }
    foreach ($name in @(
        'owner_session_lifetime_seconds',
        'retention_days',
        'max_parallelism',
        'reorientation_max_tool_rounds'
    )) {
        if ($input.nanihold_runtime.$name -isnot [int] -or
            $input.nanihold_runtime.$name -le 0) {
            throw "Nanihold runtime $name must be a positive integer."
        }
    }
    if ($input.nanihold_runtime.max_parallelism -gt 32 -or
        $input.nanihold_runtime.reorientation_max_tool_rounds -gt 100) {
        throw 'Nanihold runtime concurrency or tool-round limit is invalid.'
    }
    foreach ($name in @(
        'server_allowed_origins',
        'authorized_device_ids',
        'sandbox_write_roots',
        'sandbox_network_destinations'
    )) {
        $values = @($input.nanihold_runtime.$name)
        if ($values.Count -eq 0 -or
            ($values | Sort-Object -Unique).Count -ne $values.Count) {
            throw "Nanihold runtime $name must be non-empty and unique."
        }
        foreach ($value in $values) {
            Assert-NonBlank -Value $value -Label "Nanihold runtime $name entry"
        }
    }
    try {
        $sandboxIssuedAt = [DateTimeOffset]::Parse(
            $input.nanihold_runtime.sandbox_issued_at
        )
        $sandboxExpiresAt = [DateTimeOffset]::Parse(
            $input.nanihold_runtime.sandbox_expires_at
        )
    }
    catch {
        throw 'Nanihold sandbox timestamps must be ISO-8601 with offsets.'
    }
    if ($sandboxIssuedAt -ge $sandboxExpiresAt) {
        throw 'Nanihold sandbox validity interval is invalid.'
    }
    Assert-ExactKeys -Value $input.route_priors `
        -Keys @('interface', 'coding') -Label 'Route priors'
    foreach ($family in @('interface', 'coding')) {
        $priors = @($input.route_priors.$family)
        if ($priors.Count -eq 0) {
            throw "Route priors $family must be non-empty."
        }
        foreach ($prior in $priors) {
            Assert-ExactKeys -Value $prior `
                -Keys @(
                    'source',
                    'benchmark_family',
                    'version',
                    'sample_count',
                    'harness',
                    'successes',
                    'failures',
                    'log_token_samples',
                    'log_cost_samples',
                    'log_latency_samples'
                ) -Label "Route prior $family"
            foreach ($name in @(
                'source',
                'benchmark_family',
                'version',
                'harness'
            )) {
                Assert-NonBlank -Value $prior.$name `
                    -Label "Route prior $family $name"
                if ($prior.$name -match '(?i)EXAMPLE|REPLACE|PLACEHOLDER') {
                    throw "Route prior $family $name contains a placeholder."
                }
            }
            if ($prior.sample_count -isnot [int] -or
                $prior.sample_count -le 0 -or
                $prior.successes -isnot [int] -or
                $prior.failures -isnot [int] -or
                $prior.successes -lt 0 -or
                $prior.failures -lt 0 -or
                ($prior.successes + $prior.failures) -ne $prior.sample_count) {
                throw "Route prior $family counts are inconsistent."
            }
            foreach ($samples in @(
                @($prior.log_token_samples),
                @($prior.log_cost_samples),
                @($prior.log_latency_samples)
            )) {
                if ($samples.Count -eq 0) {
                    throw "Route prior $family requires measured log samples."
                }
                foreach ($sample in $samples) {
                    if ($sample -isnot [double] -and $sample -isnot [int]) {
                        throw "Route prior $family has a non-numeric sample."
                    }
                }
            }
        }
    }
    if ($input.lethe_data_space_id -cnotmatch '^data-space:[A-Za-z0-9._~-]{1,160}$') {
        throw 'LETHE DataSpace ID is invalid.'
    }
    foreach ($name in @('postgres_storage_gib', 'blob_storage_gib')) {
        if ($input.$name -isnot [int] -or $input.$name -lt 32) {
            throw "Deployment input $name must be an integer of at least 32 GiB."
        }
    }
    if ($input.codex_effort -cne 'xhigh') {
        throw 'Codex coding S1 effort must be xhigh.'
    }
    if ($input.pilot_permission_mode -cnotin @(
        'sandboxed_bypass',
        'managed_permissions',
        'observe_only'
    )) {
        throw 'Pilot permission mode is invalid.'
    }
    if ($input.pilot_permission_mode -ceq 'sandboxed_bypass' -and
        $input.pilot_sandbox_certificate_sha256 -cnotmatch '^[0-9a-f]{64}$') {
        throw 'sandboxed_bypass requires a SandboxProfile certificate.'
    }
    if ($input.pilot_permission_mode -cne 'sandboxed_bypass' -and
        $null -ne $input.pilot_sandbox_certificate_sha256) {
        throw 'SandboxProfile certificate is valid only for sandboxed_bypass.'
    }
    foreach ($name in @(
        'pilot_timeout_seconds',
        'codex_max_input_tokens',
        'codex_max_output_tokens',
        'codex_max_total_tokens'
    )) {
        if ($input.$name -isnot [int] -or $input.$name -le 0) {
            throw "Deployment input $name must be a positive integer."
        }
    }
    if ($input.codex_max_total_tokens -lt
        ($input.codex_max_input_tokens + $input.codex_max_output_tokens)) {
        throw 'Codex total token budget must cover input and output budgets.'
    }
    if ($input.claude_max_budget_usd -isnot [double] -and
        $input.claude_max_budget_usd -isnot [int]) {
        throw 'Claude max budget must be numeric.'
    }
    if ($input.claude_max_budget_usd -le 0) {
        throw 'Claude max budget must be positive.'
    }
    Assert-FileDigest -Path $input.k3s_install_script_path `
        -Expected $input.k3s_install_script_sha256 -Label 'k3s install script'
    Assert-FileDigest -Path $input.k3s_binary_path `
        -Expected $input.k3s_binary_sha256 -Label 'k3s binary'
    Assert-FileDigest -Path $input.oscdimg_path `
        -Expected $input.oscdimg_sha256 -Label 'oscdimg'
    Assert-FileDigest -Path $input.longhorn_manifest_path `
        -Expected $input.longhorn_manifest_sha256 -Label 'Longhorn manifest'
    Assert-FileDigest -Path $input.cloudnative_pg_manifest_path `
        -Expected $input.cloudnative_pg_manifest_sha256 -Label 'CloudNativePG manifest'
    Assert-FileDigest -Path $input.monitoring_manifest_path `
        -Expected $input.monitoring_manifest_sha256 -Label 'Monitoring manifest'
    Assert-FileDigest -Path $input.backup_image_archive_path `
        -Expected $input.backup_image_archive_sha256 -Label 'Backup image archive'
    Assert-FileDigest -Path $input.runtime_contract_receipt_path `
        -Expected $input.runtime_contract_receipt_sha256 `
        -Label 'Runtime contract receipt'
    $null = Read-RuntimeContractReceipt -Deployment $input
    return $input
}

function Read-RuntimeContractReceipt {
    param(
        [Parameter(Mandatory)][object] $Deployment
    )
    $keys = @(
        'schema_version',
        'status',
        'nanihold_image',
        'lethe_image',
        'backup_image',
        'nanihold_source_commit',
        'lethe_source_commit',
        'backup_source_commit',
        'capabilities',
        'test_receipt_sha256'
    )
    $receipt = Read-ExactJson -Path $Deployment.runtime_contract_receipt_path `
        -Keys $keys -Label 'Runtime contract receipt'
    $required = @(
        'nanihold-production-config-v1',
        'pilot-host-http-active-standby-v1',
        'lethe-serve-event-ledger-v1',
        'lethe-serve-projection-v1',
        'lethe-projection-health-v1',
        'lethe-restore-state-v1',
        'lethe-ha-canary-v1',
        'backup-canonical-event-blob-projection-postgres-v1'
    )
    if ($receipt.schema_version -ne 1 -or
        $receipt.status -cne 'implemented-verified' -or
        $receipt.nanihold_image -cne $Deployment.nanihold_image -or
        $receipt.lethe_image -cne $Deployment.lethe_image -or
        $receipt.backup_image -cne $Deployment.backup_image -or
        (@($receipt.capabilities | Sort-Object) -join ',') -cne
        (@($required | Sort-Object) -join ',')) {
        throw (
            'RUNTIME_CONTRACT_UNAVAILABLE: built images do not have the exact ' +
            'verified Nanihold/LETHE/backup capabilities required by HA.'
        )
    }
    foreach ($name in @(
        'nanihold_source_commit',
        'lethe_source_commit',
        'backup_source_commit',
        'test_receipt_sha256'
    )) {
        if ($receipt.$name -isnot [string] -or
            $receipt.$name -cnotmatch '^[0-9a-f]{40,64}$') {
            throw "Runtime contract receipt $name is not an exact source/test digest."
        }
    }
    return $receipt
}

function Read-SecretInput {
    param([Parameter(Mandatory)][string] $Path)
    $keys = @(
        'schema_version',
        'nanihold_k3s_token',
        'lethe_k3s_token',
        'postgres_superuser_password',
        'postgres_replication_password',
        'lethe_encryption_key',
        'lethe_history_bearer_token',
        'nanihold_api_bearer_token',
        'pilot_host_bearer_token',
        'mcp_gateway_bearer_token',
        'anthropic_api_key',
        'openai_api_key',
        'backup_encryption_key'
    )
    $input = Read-ExactJson -Path $Path -Keys $keys -Label 'Secret input'
    if ($input.schema_version -ne 1) {
        throw 'Unsupported secret input schema.'
    }
    foreach ($name in $keys | Where-Object { $_ -ne 'schema_version' }) {
        $value = $input.$name
        if ($value -isnot [string] -or $value.Length -lt 32) {
            throw "Secret input $name must contain at least 32 characters."
        }
        if ($value -match '(?i)(replace|changeme|example|placeholder|default)') {
            throw "Secret input $name contains a forbidden placeholder."
        }
    }
    if (Test-WindowsPlatform) {
        $acl = Get-Acl -LiteralPath $Path
        $broad = @(
            $acl.Access | Where-Object {
                $_.AccessControlType -eq 'Allow' -and
                $_.IdentityReference.Value -match '(?i)(Everyone|BUILTIN\\Users|Authenticated Users)'
            }
        )
        if ($broad.Count -ne 0) {
            throw 'Secret input ACL grants access to a broad principal.'
        }
    }
    return $input
}

function Read-HaTopology {
    param([Parameter(Mandatory)][string] $Path)
    Assert-File -Path $Path -Label 'Topology'
    $topology = Import-PowerShellDataFile -LiteralPath $Path
    $keys = @(
        'SchemaVersion',
        'SwitchName',
        'Gateway',
        'ExistingMcpGateway',
        'PrefixLength',
        'DnsServers',
        'VmRoot',
        'Availability',
        'Clusters',
        'Nodes'
    )
    Assert-ExactKeys -Value ([pscustomobject]$topology) -Keys $keys -Label 'Topology'
    if ($topology.SchemaVersion -ne 2) {
        throw 'Unsupported topology schema.'
    }
    if ($topology.SwitchName -cne 'mcp-internal' -or
        $topology.ExistingMcpGateway -cne '172.31.100.10') {
        throw 'Topology changed the existing MCP network boundary.'
    }
    if ($topology.Availability.RpoSeconds -ne 0 -or
        $topology.Availability.RtoSeconds -ne 300) {
        throw 'Topology availability target must be RPO 0 and RTO 300 seconds.'
    }
    $clusters = @($topology.Clusters)
    if ($clusters.Count -ne 2 -or
        (@($clusters.Name | Sort-Object) -join ',') -cne 'lethe,nanihold') {
        throw 'Topology requires exactly separate Nanihold and LETHE clusters.'
    }
    $nanihold = $clusters | Where-Object Name -EQ 'nanihold'
    $lethe = $clusters | Where-Object Name -EQ 'lethe'
    if ($nanihold.Vip -cne '172.31.100.20' -or
        $lethe.Vip -cne '172.31.100.30') {
        throw 'Topology service VIPs are immutable.'
    }
    if ($nanihold.PodCidr -ceq $lethe.PodCidr -or
        $nanihold.ServiceCidr -ceq $lethe.ServiceCidr) {
        throw 'Nanihold and LETHE cluster CIDRs must be separate.'
    }
    $nodes = @($topology.Nodes)
    if ($nodes.Count -ne 6) {
        throw 'Topology requires exactly six VMs.'
    }
    $names = @($nodes.Name)
    $addresses = @($nodes.Address)
    if (($names | Sort-Object -Unique).Count -ne $names.Count -or
        ($addresses | Sort-Object -Unique).Count -ne $addresses.Count) {
        throw 'Topology node names and addresses must be unique.'
    }
    foreach ($cluster in $clusters) {
        $members = @($nodes | Where-Object Cluster -EQ $cluster.Name)
        if ($members.Count -ne 3 -or
            (@($members.Name | Sort-Object) -join ',') -cne
            (@($cluster.Nodes | Sort-Object) -join ',')) {
            throw "Cluster membership mismatch: $($cluster.Name)"
        }
    }
    return $topology
}

function Assert-NasWritable {
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "NAS backup path is unavailable: $Path"
    }
    $probe = Join-Path $Path ".nanihold-write-probe-$([guid]::NewGuid().ToString('N'))"
    try {
        $stream = [IO.File]::Open(
            $probe,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $stream.WriteByte(0x4e)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
    }
    catch {
        throw "NAS backup path is not durably writable: $Path"
    }
    finally {
        if (Test-Path -LiteralPath $probe -PathType Leaf) {
            Remove-Item -LiteralPath $probe -Force
        }
    }
}

function Assert-PrivateAcl {
    param([Parameter(Mandatory)][string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Private path does not exist: $Path"
    }
    if (Test-WindowsPlatform) {
        $acl = Get-Acl -LiteralPath $Path
        $broad = @(
            $acl.Access | Where-Object {
                $_.AccessControlType -eq 'Allow' -and
                $_.IdentityReference.Value -match '(?i)(Everyone|BUILTIN\\Users|Authenticated Users)'
            }
        )
        if ($broad.Count -ne 0) {
            throw "Private path ACL grants access to a broad principal: $Path"
        }
    }
}

function Read-PreflightReceipt {
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $TopologyPath,
        [Parameter(Mandatory)][string] $DeploymentInputPath
    )
    $keys = @(
        'schema_version',
        'status',
        'created_at',
        'host',
        'topology_sha256',
        'deployment_input_sha256',
        'ubuntu_iso_sha256',
        'ssh_public_key_sha256',
        'nas_path',
        'switch_name',
        'node_count',
        'nanihold_vip',
        'lethe_vip',
        'required_memory_gib',
        'required_disk_gib'
    )
    $receipt = Read-ExactJson -Path $Path -Keys $keys -Label 'Preflight receipt'
    if ($receipt.schema_version -ne 1 -or $receipt.status -cne 'ready') {
        throw 'Preflight receipt is not ready.'
    }
    if ($receipt.host -cne [Environment]::MachineName) {
        throw 'Preflight receipt belongs to a different host.'
    }
    if ($receipt.topology_sha256 -cne (Get-FileSha256 -Path $TopologyPath) -or
        $receipt.deployment_input_sha256 -cne
        (Get-FileSha256 -Path $DeploymentInputPath)) {
        throw 'Preflight receipt input digest mismatch.'
    }
    return $receipt
}

Export-ModuleMember -Function @(
    'Assert-Administrator',
    'Assert-ExactKeys',
    'Assert-File',
    'Assert-FileDigest',
    'Assert-NasWritable',
    'Assert-PrivateAcl',
    'Get-FileSha256',
    'Read-DeploymentInput',
    'Read-HaTopology',
    'Read-PreflightReceipt',
    'Read-RuntimeContractReceipt',
    'Read-SecretInput'
)
