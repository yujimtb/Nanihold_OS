[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Plan', 'Apply')]
    [string] $Mode,

    [Parameter(Mandatory)]
    [ValidateSet('INTERRUPT_ONE_VERIFIED_HA_MEMBER')]
    [string] $RiskAcceptance,

    [Parameter(Mandatory)]
    [ValidateSet('Nanihold', 'Lethe')]
    [string] $Service,

    [Parameter(Mandatory)]
    [ValidateSet('Pod', 'VM')]
    [string] $FaultKind,

    [Parameter(Mandatory)]
    [string] $Target,

    [Parameter()]
    [string] $TopologyPath = (Join-Path $PSScriptRoot 'topology.psd1'),

    [Parameter(Mandatory)]
    [string] $DeploymentInputPath,

    [Parameter(Mandatory)]
    [string] $SecretInputPath,

    [Parameter(Mandatory)]
    [string] $VerificationReceiptPath,

    [Parameter(Mandatory)]
    [string] $NaniholdKubeconfigPath,

    [Parameter(Mandatory)]
    [string] $LetheKubeconfigPath,

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
        throw 'kubectl failover measurement command failed.'
    }
    return $output | ConvertFrom-Json -Depth 64
}

function Assert-CanaryState {
    param(
        [Parameter(Mandatory)][object] $State,
        [Parameter(Mandatory)][string] $CanaryId,
        [Parameter(Mandatory)][string] $DataSpaceId,
        [Parameter(Mandatory)][string] $PayloadSha256
    )
    Assert-ExactKeys -Value $State `
        -Keys @(
            'schema_version',
            'canary_id',
            'data_space_id',
            'event_cursor',
            'event_count',
            'blob_sha256',
            'projection_cursor',
            'projection_status'
        ) -Label 'HA canary state'
    if ($State.schema_version -ne 1 -or
        $State.canary_id -cne $CanaryId -or
        $State.data_space_id -cne $DataSpaceId -or
        $State.event_count -ne 1 -or
        $State.blob_sha256 -cne $PayloadSha256 -or
        $State.projection_status -cne 'materialized' -or
        $State.projection_cursor -lt $State.event_cursor) {
        throw 'HA canary is not durably present in Event, blob, and Projection.'
    }
}

$topology = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
$secrets = Read-SecretInput -Path $SecretInputPath
Assert-File -Path $VerificationReceiptPath -Label 'Verification receipt'
Assert-File -Path $NaniholdKubeconfigPath -Label 'Nanihold kubeconfig'
Assert-File -Path $LetheKubeconfigPath -Label 'LETHE kubeconfig'
Assert-PrivateAcl -Path $NaniholdKubeconfigPath
Assert-PrivateAcl -Path $LetheKubeconfigPath
if (Test-Path -LiteralPath $OutputReceiptPath) {
    throw "Failover measurement receipt already exists: $OutputReceiptPath"
}
$verification = Get-Content -LiteralPath $VerificationReceiptPath `
    -Raw -Encoding UTF8 | ConvertFrom-Json -Depth 16
if ($verification.status -cne 'verified' -or
    $verification.topology_sha256 -cne (Get-FileSha256 -Path $TopologyPath) -or
    $verification.deployment_input_sha256 -cne
    (Get-FileSha256 -Path $DeploymentInputPath)) {
    throw 'Verification receipt does not authorize fault injection.'
}

$clusterName = $Service.ToLowerInvariant()
$cluster = $topology.Clusters | Where-Object Name -CEQ $clusterName
$kubeconfig = if ($clusterName -ceq 'nanihold') {
    $NaniholdKubeconfigPath
}
else {
    $LetheKubeconfigPath
}
$namespace = if ($clusterName -ceq 'nanihold') {
    'nanihold-system'
}
else {
    'lethe-system'
}

if ($FaultKind -ceq 'VM') {
    Assert-Administrator
    $node = @(
        $topology.Nodes |
        Where-Object {
            $_.Name -ceq $Target -and $_.Cluster -ceq $clusterName
        }
    )
    if ($node.Count -ne 1) {
        throw 'Fault target is not exactly one member of the selected cluster.'
    }
    $vm = Get-VM -Name $Target -ErrorAction Stop
    if ($vm.State -cne 'Running') {
        throw 'VM fault target is not running.'
    }
}
else {
    $pod = Invoke-KubectlJson -Kubeconfig $kubeconfig `
        -Arguments @('get', 'pod', $Target, '--namespace', $namespace)
    $allowedApps = if ($clusterName -ceq 'nanihold') {
        @('nanihold', 'pilot-host')
    }
    else {
        @('lethe-event-api', 'lethe-projection', 'lethe-postgres')
    }
    $appProperty = $pod.metadata.labels.PSObject.Properties['app']
    $clusterProperty = $pod.metadata.labels.PSObject.Properties['cnpg.io/cluster']
    $appAllowed = $null -ne $appProperty -and
        $appProperty.Value -cin $allowedApps
    $postgresAllowed = $clusterName -ceq 'lethe' -and
        $null -ne $clusterProperty -and
        $clusterProperty.Value -ceq 'lethe-postgres'
    if ($pod.metadata.name -cne $Target -or
        $pod.metadata.namespace -cne $namespace -or
        (-not $appAllowed -and -not $postgresAllowed)) {
        throw 'Pod fault target is outside the selected HA service boundary.'
    }
}

$plan = [pscustomobject]@{
    Status = 'plan-only'
    MutationPerformed = $false
    Service = $Service
    FaultKind = $FaultKind
    Target = $Target
    RpoTargetSeconds = $topology.Availability.RpoSeconds
    RtoTargetSeconds = $topology.Availability.RtoSeconds
}
if ($Mode -ceq 'Plan') {
    return $plan
}

$lethe = $topology.Clusters | Where-Object Name -CEQ 'lethe'
$canaryId = "ha-$([guid]::NewGuid().ToString('N'))"
$payload = [byte[]]::new(64)
$random = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $random.GetBytes($payload)
}
finally {
    $random.Dispose()
}
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $payloadSha256 = (
        [BitConverter]::ToString($sha.ComputeHash($payload))
    ).Replace('-', '').ToLowerInvariant()
}
finally {
    $sha.Dispose()
}
$canaryBody = [ordered]@{
    schema_version = 1
    canary_id = $canaryId
    idempotency_key = $canaryId
    data_space_id = $deployment.lethe_data_space_id
    payload_base64 = [Convert]::ToBase64String($payload)
    payload_sha256 = $payloadSha256
} | ConvertTo-Json -Depth 8
$letheHeaders = @{
    Authorization = "Bearer $($secrets.lethe_history_bearer_token)"
    'X-Data-Space-Id' = $deployment.lethe_data_space_id
}
$before = Invoke-RestMethod -Method Post `
    -Uri "https://$($lethe.Vip)/api/ha/canaries" `
    -Headers $letheHeaders `
    -ContentType 'application/json' `
    -Body $canaryBody
Assert-CanaryState -State $before -CanaryId $canaryId `
    -DataSpaceId $deployment.lethe_data_space_id `
    -PayloadSha256 $payloadSha256

$faultStarted = [Diagnostics.Stopwatch]::StartNew()
if ($FaultKind -ceq 'VM') {
    Stop-VM -Name $Target -TurnOff -Confirm:$false -ErrorAction Stop
    Start-VM -Name $Target -ErrorAction Stop
}
else {
    & kubectl --kubeconfig $kubeconfig delete pod $Target `
        --namespace $namespace --wait=false
    if ($LASTEXITCODE -ne 0) {
        throw 'Pod fault injection failed.'
    }
}

$healthUri = "https://$($cluster.Vip)/health/ready"
$healthToken = if ($clusterName -ceq 'nanihold') {
    $secrets.nanihold_api_bearer_token
}
else {
    $secrets.lethe_history_bearer_token
}
$consecutiveReady = 0
$lastError = $null
while ($faultStarted.Elapsed.TotalSeconds -le $topology.Availability.RtoSeconds) {
    try {
        $health = Invoke-RestMethod -Method Get -Uri $healthUri `
            -Headers @{ Authorization = "Bearer $healthToken" } `
            -TimeoutSec 2
        $isReady = if ($clusterName -ceq 'nanihold') {
            $health.status -ceq 'ready'
        }
        else {
            $health.event_ledger.status -ceq 'ready' -and
            $health.blob_store.status -ceq 'ready' -and
            $health.projection.status -ceq 'ready'
        }
        if ($isReady) {
            $consecutiveReady += 1
            if ($consecutiveReady -eq 3) {
                break
            }
        }
        else {
            $consecutiveReady = 0
        }
    }
    catch {
        $lastError = $_.Exception.Message
        $consecutiveReady = 0
    }
    Start-Sleep -Milliseconds 1000
}
$faultStarted.Stop()
if ($consecutiveReady -ne 3) {
    throw "Service did not recover within RTO: $lastError"
}
$rtoMilliseconds = [long][Math]::Ceiling($faultStarted.Elapsed.TotalMilliseconds)

$after = Invoke-RestMethod -Method Get `
    -Uri "https://$($lethe.Vip)/api/ha/canaries/$canaryId" `
    -Headers $letheHeaders
Assert-CanaryState -State $after -CanaryId $canaryId `
    -DataSpaceId $deployment.lethe_data_space_id `
    -PayloadSha256 $payloadSha256
if ($after.event_cursor -ne $before.event_cursor -or
    $after.event_count -ne $before.event_count -or
    $after.blob_sha256 -cne $before.blob_sha256) {
    throw 'Failover changed or duplicated the durable HA canary.'
}
if ($rtoMilliseconds -gt ($topology.Availability.RtoSeconds * 1000)) {
    throw 'Measured RTO exceeds the topology contract.'
}

$receipt = [ordered]@{
    schema_version = 1
    status = 'failover-verified'
    created_at = [DateTimeOffset]::UtcNow.ToString('o')
    service = $Service
    fault_kind = $FaultKind
    target = $Target
    canary_id = $canaryId
    canary_sha256 = $payloadSha256
    event_cursor = [long]$after.event_cursor
    projection_cursor = [long]$after.projection_cursor
    rpo_seconds = 0
    rto_milliseconds = $rtoMilliseconds
    topology_sha256 = Get-FileSha256 -Path $TopologyPath
    deployment_input_sha256 = Get-FileSha256 -Path $DeploymentInputPath
    verification_receipt_sha256 = Get-FileSha256 -Path $VerificationReceiptPath
}
$output = [IO.Path]::GetFullPath($OutputReceiptPath)
$outputParent = Split-Path -Parent $output
if (-not (Test-Path -LiteralPath $outputParent -PathType Container)) {
    throw "Failover receipt parent does not exist: $outputParent"
}
$stream = [IO.File]::Open(
    $output,
    [IO.FileMode]::CreateNew,
    [IO.FileAccess]::Write,
    [IO.FileShare]::None
)
$receiptJson = $receipt | ConvertTo-Json -Depth 8
try {
    $writer = [IO.StreamWriter]::new(
        $stream,
        [Text.UTF8Encoding]::new($false)
    )
    try {
        $writer.Write($receiptJson)
        $writer.Flush()
        $stream.Flush($true)
    }
    finally {
        $writer.Dispose()
    }
}
finally {
    $stream.Dispose()
}
[pscustomobject]$receipt
