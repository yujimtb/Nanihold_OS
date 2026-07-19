[CmdletBinding()]
param(
    [Parameter()]
    [string] $TopologyPath = (Join-Path $PSScriptRoot 'topology.psd1'),

    [Parameter(Mandatory)]
    [string] $DeploymentInputPath,

    [Parameter(Mandatory)]
    [string] $SecretInputPath,

    [Parameter(Mandatory)]
    [string] $LetheKubeconfigPath,

    [Parameter(Mandatory)]
    [string] $OutputReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'HaContract.psm1') -Force

$topology = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
$secrets = Read-SecretInput -Path $SecretInputPath
Assert-File -Path $LetheKubeconfigPath -Label 'LETHE kubeconfig'
Assert-PrivateAcl -Path $LetheKubeconfigPath
if (Test-Path -LiteralPath $OutputReceiptPath) {
    throw "Empty-target receipt already exists: $OutputReceiptPath"
}

$lethe = $topology.Clusters | Where-Object Name -CEQ 'lethe'
$state = Invoke-RestMethod -Method Get `
    -Uri "https://$($lethe.Vip)/api/restore/state" `
    -Headers @{
        Authorization = "Bearer $($secrets.lethe_history_bearer_token)"
        'X-Data-Space-Id' = $deployment.lethe_data_space_id
    }
Assert-ExactKeys -Value $state `
    -Keys @(
        'schema_version',
        'data_space_id',
        'event_count',
        'event_cursor',
        'blob_count',
        'projection_count',
        'projection_cursor'
    ) -Label 'LETHE restore state'
if ($state.schema_version -ne 1 -or
    $state.data_space_id -cne $deployment.lethe_data_space_id -or
    $state.event_count -ne 0 -or
    $state.event_cursor -ne 0 -or
    $state.blob_count -ne 0 -or
    $state.projection_count -ne 0 -or
    $state.projection_cursor -ne 0) {
    throw 'LETHE restore target is not empty.'
}

$receipt = [ordered]@{
    schema_version = 1
    status = 'empty-target-verified'
    created_at = [DateTimeOffset]::UtcNow.ToString('o')
    data_space_id = $deployment.lethe_data_space_id
    event_count = 0
    blob_count = 0
    projection_count = 0
}
$output = [IO.Path]::GetFullPath($OutputReceiptPath)
$parent = Split-Path -Parent $output
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    throw "Empty-target receipt parent does not exist: $parent"
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
