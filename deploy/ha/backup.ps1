[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Plan', 'Apply')]
    [string] $Mode,

    [Parameter()]
    [string] $TopologyPath = (Join-Path $PSScriptRoot 'topology.psd1'),

    [Parameter(Mandatory)]
    [string] $DeploymentInputPath,

    [Parameter(Mandatory)]
    [string] $SecretInputPath,

    [Parameter(Mandatory)]
    [string] $VerificationReceiptPath,

    [Parameter(Mandatory)]
    [string] $LetheKubeconfigPath,

    [Parameter(Mandatory)]
    [string] $NasBackupPath,

    [Parameter(Mandatory)]
    [int] $TimeoutSeconds,

    [Parameter(Mandatory)]
    [string] $OutputReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'HaContract.psm1') -Force

$null = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
$null = Read-SecretInput -Path $SecretInputPath
Assert-File -Path $VerificationReceiptPath -Label 'Verification receipt'
Assert-File -Path $LetheKubeconfigPath -Label 'LETHE kubeconfig'
Assert-PrivateAcl -Path $LetheKubeconfigPath
Assert-NasWritable -Path $NasBackupPath
if ($TimeoutSeconds -le 0) {
    throw 'Backup timeout must be positive.'
}
if (Test-Path -LiteralPath $OutputReceiptPath) {
    throw "Backup receipt already exists: $OutputReceiptPath"
}
$verification = Get-Content -LiteralPath $VerificationReceiptPath `
    -Raw -Encoding UTF8 | ConvertFrom-Json -Depth 16
if ($verification.status -cne 'verified' -or
    $verification.topology_sha256 -cne (Get-FileSha256 -Path $TopologyPath) -or
    $verification.deployment_input_sha256 -cne
    (Get-FileSha256 -Path $DeploymentInputPath)) {
    throw 'Verification receipt does not authorize backup.'
}

$before = @(
    Get-ChildItem -LiteralPath $NasBackupPath -Directory -Filter 'backup-*' |
    ForEach-Object FullName
)
if ($Mode -ceq 'Plan') {
    return [pscustomobject]@{
        Status = 'plan-only'
        MutationPerformed = $false
        DataSpaceId = $deployment.lethe_data_space_id
        Nas = [IO.Path]::GetFullPath($NasBackupPath)
    }
}

$jobName = "lethe-backup-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
& kubectl --kubeconfig $LetheKubeconfigPath create job $jobName `
    --namespace lethe-system --from=cronjob/lethe-canonical-backup
if ($LASTEXITCODE -ne 0) {
    throw 'Could not create the LETHE backup Job.'
}
& kubectl --kubeconfig $LetheKubeconfigPath wait `
    --namespace lethe-system --for=condition=Complete "job/$jobName" `
    "--timeout=${TimeoutSeconds}s"
if ($LASTEXITCODE -ne 0) {
    throw 'LETHE backup Job did not complete before the deadline.'
}

$after = @(
    Get-ChildItem -LiteralPath $NasBackupPath -Directory -Filter 'backup-*' |
    ForEach-Object FullName
)
$newBackups = @($after | Where-Object { $_ -cnotin $before })
if ($newBackups.Count -ne 1) {
    throw 'Backup Job did not create exactly one new NAS backup directory.'
}
$manifestPath = Join-Path $newBackups[0] 'manifest.json'
$manifestKeys = @(
    'schema_version',
    'backup_id',
    'completed_at',
    'data_space_id',
    'event_cursor',
    'event_count',
    'event_export_sha256',
    'blob_manifest_sha256',
    'blob_count',
    'projection_cursor',
    'postgres_backup_sha256',
    'signature_sha256'
)
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 |
    ConvertFrom-Json -Depth 16
Assert-ExactKeys -Value $manifest -Keys $manifestKeys -Label 'Backup manifest'
if ($manifest.schema_version -ne 1 -or
    $manifest.data_space_id -cne $deployment.lethe_data_space_id) {
    throw 'Backup manifest DataSpace or schema mismatch.'
}
foreach ($name in @(
    'event_export_sha256',
    'blob_manifest_sha256',
    'postgres_backup_sha256',
    'signature_sha256'
)) {
    if ($manifest.$name -isnot [string] -or
        $manifest.$name -cnotmatch '^[0-9a-f]{64}$') {
        throw "Backup manifest digest is invalid: $name"
    }
}
foreach ($name in @(
    'event_cursor',
    'event_count',
    'blob_count',
    'projection_cursor'
)) {
    if ($manifest.$name -isnot [long] -and $manifest.$name -isnot [int]) {
        throw "Backup manifest count is invalid: $name"
    }
    if ($manifest.$name -lt 0) {
        throw "Backup manifest count is negative: $name"
    }
}
if ($manifest.projection_cursor -ne $manifest.event_cursor) {
    throw 'Backup Projection cursor differs from Event Ledger cursor.'
}

$receipt = [ordered]@{
    schema_version = 1
    status = 'backup-verified'
    created_at = [DateTimeOffset]::UtcNow.ToString('o')
    backup_id = $manifest.backup_id
    data_space_id = $manifest.data_space_id
    event_cursor = $manifest.event_cursor
    event_count = $manifest.event_count
    blob_count = $manifest.blob_count
    projection_cursor = $manifest.projection_cursor
    manifest_path = [IO.Path]::GetFullPath($manifestPath)
    manifest_sha256 = Get-FileSha256 -Path $manifestPath
    verification_receipt_sha256 = Get-FileSha256 -Path $VerificationReceiptPath
}
[IO.File]::WriteAllText(
    [IO.Path]::GetFullPath($OutputReceiptPath),
    ($receipt | ConvertTo-Json -Depth 8),
    [Text.UTF8Encoding]::new($false)
)
[pscustomobject]$receipt
