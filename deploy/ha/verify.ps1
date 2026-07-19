[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Static', 'Live')]
    [string] $Mode,

    [Parameter()]
    [string] $TopologyPath = (Join-Path $PSScriptRoot 'topology.psd1'),

    [Parameter(Mandatory)]
    [string] $DeploymentInputPath,

    [Parameter(Mandatory)]
    [string] $SecretInputPath,

    [Parameter(Mandatory)]
    [string] $BootstrapReceiptPath,

    [Parameter(Mandatory)]
    [string] $NaniholdKubeconfigPath,

    [Parameter(Mandatory)]
    [string] $LetheKubeconfigPath,

    [Parameter(Mandatory)]
    [string] $NasBackupPath,

    [Parameter(Mandatory)]
    [string] $OutputReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'HaContract.psm1') -Force

function Invoke-KubectlJson {
    param(
        [Parameter(Mandatory)][string] $Kubeconfig,
        [Parameter(Mandatory)][string[]] $Arguments
    )
    $output = & kubectl --kubeconfig $Kubeconfig @Arguments -o json
    if ($LASTEXITCODE -ne 0) {
        throw 'kubectl verification command failed.'
    }
    return $output | ConvertFrom-Json -Depth 64
}

function Assert-ClusterNodes {
    param(
        [Parameter(Mandatory)][string] $Kubeconfig,
        [Parameter(Mandatory)][string[]] $ExpectedNames
    )
    $nodes = Invoke-KubectlJson -Kubeconfig $Kubeconfig -Arguments @('get', 'nodes')
    $items = @($nodes.items)
    if ($items.Count -ne 3 -or
        (@($items.metadata.name | Sort-Object) -join ',') -cne
        (@($ExpectedNames | Sort-Object) -join ',')) {
        throw 'k3s node inventory differs from topology.'
    }
    foreach ($node in $items) {
        $ready = @(
            $node.status.conditions |
            Where-Object { $_.type -ceq 'Ready' -and $_.status -ceq 'True' }
        )
        if ($ready.Count -ne 1) {
            throw "k3s node is not Ready: $($node.metadata.name)"
        }
    }
}

function Assert-DeploymentAvailable {
    param(
        [Parameter(Mandatory)][string] $Kubeconfig,
        [Parameter(Mandatory)][string] $Namespace,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][int] $MinimumAvailable
    )
    $deployment = Invoke-KubectlJson -Kubeconfig $Kubeconfig `
        -Arguments @('get', 'deployment', $Name, '--namespace', $Namespace)
    if ([int]$deployment.status.availableReplicas -lt $MinimumAvailable) {
        throw "Deployment is below availability gate: $Namespace/$Name"
    }
}

function Assert-BackupImageContract {
    param(
        [Parameter(Mandatory)][string] $Kubeconfig,
        [Parameter(Mandatory)][string] $Image
    )
    $jobName = "backup-contract-$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())"
    $job = [ordered]@{
        apiVersion = 'batch/v1'
        kind = 'Job'
        metadata = [ordered]@{
            name = $jobName
            namespace = 'lethe-system'
        }
        spec = [ordered]@{
            backoffLimit = 0
            template = [ordered]@{
                spec = [ordered]@{
                    restartPolicy = 'Never'
                    containers = @(
                        [ordered]@{
                            name = 'contract'
                            image = $Image
                            imagePullPolicy = 'IfNotPresent'
                            args = @('contract', '--format', 'json')
                        }
                    )
                }
            }
        }
    }
    try {
        $jobJson = $job | ConvertTo-Json -Depth 24
        $jobJson | & kubectl --kubeconfig $Kubeconfig apply -f -
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not create backup image contract probe.'
        }
        & kubectl --kubeconfig $Kubeconfig wait `
            --namespace lethe-system --for=condition=Complete "job/$jobName" `
            --timeout=120s
        if ($LASTEXITCODE -ne 0) {
            throw 'Backup image contract probe did not complete.'
        }
        $contractJson = & kubectl --kubeconfig $Kubeconfig logs `
            --namespace lethe-system "job/$jobName"
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not read backup image contract.'
        }
        try {
            $contract = $contractJson | ConvertFrom-Json -Depth 16
        }
        catch {
            throw 'Backup image contract is not valid JSON.'
        }
        Assert-ExactKeys -Value $contract `
            -Keys @(
                'schema_version',
                'commands',
                'mode',
                'manifest_schema_version',
                'require_empty_target'
            ) -Label 'Backup image contract'
        if ($contract.schema_version -ne 1 -or
            (@($contract.commands | Sort-Object) -join ',') -cne 'backup,restore' -or
            $contract.mode -cne 'canonical-event-blob-projection-postgres' -or
            $contract.manifest_schema_version -ne 1 -or
            $contract.require_empty_target -ne $true) {
            throw 'Backup image does not implement the required backup/restore contract.'
        }
    }
    finally {
        & kubectl --kubeconfig $Kubeconfig delete job $jobName `
            --namespace lethe-system --wait=true
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not remove backup image contract probe.'
        }
    }
}

$topology = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
$secrets = Read-SecretInput -Path $SecretInputPath
Assert-File -Path $BootstrapReceiptPath -Label 'Bootstrap receipt'
Assert-File -Path $NaniholdKubeconfigPath -Label 'Nanihold kubeconfig'
Assert-File -Path $LetheKubeconfigPath -Label 'LETHE kubeconfig'
Assert-PrivateAcl -Path $NaniholdKubeconfigPath
Assert-PrivateAcl -Path $LetheKubeconfigPath
Assert-NasWritable -Path $NasBackupPath
if (Test-Path -LiteralPath $OutputReceiptPath) {
    throw "Verification receipt already exists: $OutputReceiptPath"
}

$bootstrap = Get-Content -LiteralPath $BootstrapReceiptPath -Raw -Encoding UTF8 |
    ConvertFrom-Json -Depth 16
Assert-ExactKeys -Value $bootstrap `
    -Keys @(
        'schema_version',
        'status',
        'created_at',
        'host',
        'topology_sha256',
        'deployment_input_sha256',
        'provision_receipt_sha256',
        'manifest_receipt_sha256',
        'nanihold_kubeconfig_sha256',
        'lethe_kubeconfig_sha256'
    ) -Label 'Bootstrap receipt'
if ($bootstrap.schema_version -ne 1 -or
    $bootstrap.status -cne 'bootstrapped' -or
    $bootstrap.topology_sha256 -cne (Get-FileSha256 -Path $TopologyPath) -or
    $bootstrap.deployment_input_sha256 -cne
    (Get-FileSha256 -Path $DeploymentInputPath) -or
    $bootstrap.nanihold_kubeconfig_sha256 -cne
    (Get-FileSha256 -Path $NaniholdKubeconfigPath) -or
    $bootstrap.lethe_kubeconfig_sha256 -cne
    (Get-FileSha256 -Path $LetheKubeconfigPath)) {
    throw 'Bootstrap receipt input or kubeconfig mismatch.'
}
$staticResult = [ordered]@{
    topology_sha256 = Get-FileSha256 -Path $TopologyPath
    deployment_input_sha256 = Get-FileSha256 -Path $DeploymentInputPath
    nanihold_kubeconfig_sha256 = Get-FileSha256 -Path $NaniholdKubeconfigPath
    lethe_kubeconfig_sha256 = Get-FileSha256 -Path $LetheKubeconfigPath
    nas_writable = $true
}
if ($Mode -ceq 'Static') {
    return [pscustomobject]@{
        Status = 'static-ready'
        MutationPerformed = $false
        Evidence = $staticResult
    }
}

Assert-ClusterNodes -Kubeconfig $NaniholdKubeconfigPath `
    -ExpectedNames @(
        ($topology.Nodes | Where-Object Cluster -CEQ 'nanihold').Name
    )
Assert-ClusterNodes -Kubeconfig $LetheKubeconfigPath `
    -ExpectedNames @(
        ($topology.Nodes | Where-Object Cluster -CEQ 'lethe').Name
    )
Assert-DeploymentAvailable -Kubeconfig $NaniholdKubeconfigPath `
    -Namespace 'nanihold-system' -Name 'nanihold' -MinimumAvailable 2
Assert-DeploymentAvailable -Kubeconfig $NaniholdKubeconfigPath `
    -Namespace 'nanihold-system' -Name 'pilot-host-a' -MinimumAvailable 1
Assert-DeploymentAvailable -Kubeconfig $NaniholdKubeconfigPath `
    -Namespace 'nanihold-system' -Name 'pilot-host-b' -MinimumAvailable 1
Assert-DeploymentAvailable -Kubeconfig $LetheKubeconfigPath `
    -Namespace 'lethe-system' -Name 'lethe-event-api' -MinimumAvailable 2
Assert-DeploymentAvailable -Kubeconfig $LetheKubeconfigPath `
    -Namespace 'lethe-system' -Name 'lethe-projection' -MinimumAvailable 2

$postgresPods = Invoke-KubectlJson -Kubeconfig $LetheKubeconfigPath `
    -Arguments @(
        'get', 'pods', '--namespace', 'lethe-system',
        '--selector', 'cnpg.io/cluster=lethe-postgres'
    )
if (@($postgresPods.items).Count -ne 3) {
    throw 'LETHE PostgreSQL does not have exactly three instances.'
}
$primary = @(
    $postgresPods.items |
    Where-Object { $_.metadata.labels.'cnpg.io/instanceRole' -ceq 'primary' }
)
if ($primary.Count -ne 1) {
    throw 'LETHE PostgreSQL primary identity is ambiguous.'
}
$postgresResult = & kubectl --kubeconfig $LetheKubeconfigPath `
    exec --namespace lethe-system $primary[0].metadata.name -- `
    psql -U postgres -Atc @'
SHOW synchronous_commit;
SELECT count(*) FROM pg_stat_replication WHERE sync_state = 'sync';
'@
if ($LASTEXITCODE -ne 0) {
    throw 'PostgreSQL synchronous replication query failed.'
}
$postgresLines = @($postgresResult | Where-Object { $_ -ne '' })
if ($postgresLines.Count -lt 2 -or
    $postgresLines[0] -cne 'remote_apply' -or
    [int]$postgresLines[1] -lt 1) {
    throw 'PostgreSQL is not providing synchronous remote_apply replication.'
}
Assert-BackupImageContract -Kubeconfig $LetheKubeconfigPath `
    -Image $deployment.backup_image

$letheHealth = Invoke-RestMethod -Method Get `
    -Uri 'https://172.31.100.30/health/ready' `
    -Headers @{ Authorization = "Bearer $($secrets.lethe_history_bearer_token)" }
foreach ($field in @('event_ledger', 'blob_store', 'projection')) {
    if ($letheHealth.PSObject.Properties.Name -cnotcontains $field -or
        $letheHealth.$field.status -cne 'ready') {
        throw "LETHE health is missing ready component: $field"
    }
}
$naniholdHealth = Invoke-RestMethod -Method Get `
    -Uri 'https://172.31.100.20/health/ready' `
    -Headers @{ Authorization = "Bearer $($secrets.nanihold_api_bearer_token)" }
if ($naniholdHealth.status -cne 'ready') {
    throw 'Nanihold VIP is not ready.'
}
$gatewayHealth = Invoke-RestMethod -Method Get `
    -Uri ([Uri]::new([Uri]$deployment.mcp_gateway_url, '/health')) `
    -Headers @{ Authorization = "Bearer $($secrets.mcp_gateway_bearer_token)" }
if ($gatewayHealth.status -cne 'ready') {
    throw 'Existing MCP Gateway is not ready.'
}

$receipt = [ordered]@{
    schema_version = 1
    status = 'verified'
    created_at = [DateTimeOffset]::UtcNow.ToString('o')
    topology_sha256 = $staticResult.topology_sha256
    deployment_input_sha256 = $staticResult.deployment_input_sha256
    bootstrap_receipt_sha256 = Get-FileSha256 -Path $BootstrapReceiptPath
    nanihold_nodes_ready = 3
    lethe_nodes_ready = 3
    postgres_instances = 3
    postgres_sync_replicas = [int]$postgresLines[1]
    postgres_synchronous_commit = $postgresLines[0]
    lethe_event_ledger = 'ready'
    lethe_blob_store = 'ready'
    lethe_projection = 'ready'
    nanihold_vip = 'ready'
    lethe_vip = 'ready'
    mcp_gateway = 'ready'
    nas_writable = $true
}
[IO.File]::WriteAllText(
    [IO.Path]::GetFullPath($OutputReceiptPath),
    ($receipt | ConvertTo-Json -Depth 8),
    [Text.UTF8Encoding]::new($false)
)
[pscustomobject]$receipt
