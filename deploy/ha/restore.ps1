[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Plan', 'Apply')]
    [string] $Mode,

    [Parameter(Mandatory)]
    [ValidateSet('RESTORE_ONLY_INTO_VERIFIED_EMPTY_LETHE')]
    [string] $RiskAcceptance,

    [Parameter()]
    [string] $TopologyPath = (Join-Path $PSScriptRoot 'topology.psd1'),

    [Parameter(Mandatory)]
    [string] $DeploymentInputPath,

    [Parameter(Mandatory)]
    [string] $SecretInputPath,

    [Parameter(Mandatory)]
    [string] $BackupReceiptPath,

    [Parameter(Mandatory)]
    [string] $EmptyTargetReceiptPath,

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

$topology = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
$secrets = Read-SecretInput -Path $SecretInputPath
Assert-File -Path $BackupReceiptPath -Label 'Backup receipt'
Assert-File -Path $EmptyTargetReceiptPath -Label 'Empty-target receipt'
Assert-File -Path $LetheKubeconfigPath -Label 'LETHE kubeconfig'
Assert-PrivateAcl -Path $LetheKubeconfigPath
Assert-NasWritable -Path $NasBackupPath
if ($TimeoutSeconds -le 0) {
    throw 'Restore timeout must be positive.'
}
if (Test-Path -LiteralPath $OutputReceiptPath) {
    throw "Restore receipt already exists: $OutputReceiptPath"
}

$backup = Get-Content -LiteralPath $BackupReceiptPath -Raw -Encoding UTF8 |
    ConvertFrom-Json -Depth 16
$backupKeys = @(
    'schema_version',
    'status',
    'created_at',
    'backup_id',
    'data_space_id',
    'event_cursor',
    'event_count',
    'blob_count',
    'projection_cursor',
    'manifest_path',
    'manifest_sha256',
    'verification_receipt_sha256'
)
Assert-ExactKeys -Value $backup -Keys $backupKeys -Label 'Backup receipt'
if ($backup.schema_version -ne 1 -or
    $backup.status -cne 'backup-verified' -or
    $backup.data_space_id -cne $deployment.lethe_data_space_id) {
    throw 'Backup receipt does not match the target DataSpace.'
}
$manifestPath = [IO.Path]::GetFullPath($backup.manifest_path)
$nasRoot = [IO.Path]::GetFullPath($NasBackupPath)
if (-not $manifestPath.StartsWith(
    $nasRoot + [IO.Path]::DirectorySeparatorChar,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw 'Backup manifest is outside the explicit NAS root.'
}
Assert-FileDigest -Path $manifestPath -Expected $backup.manifest_sha256 `
    -Label 'Restore backup manifest'

$emptyTarget = Get-Content -LiteralPath $EmptyTargetReceiptPath `
    -Raw -Encoding UTF8 | ConvertFrom-Json -Depth 16
Assert-ExactKeys -Value $emptyTarget `
    -Keys @(
        'schema_version',
        'status',
        'created_at',
        'data_space_id',
        'event_count',
        'blob_count',
        'projection_count'
    ) -Label 'Empty-target receipt'
if ($emptyTarget.schema_version -ne 1 -or
    $emptyTarget.status -cne 'empty-target-verified' -or
    $emptyTarget.data_space_id -cne $deployment.lethe_data_space_id -or
    $emptyTarget.event_count -ne 0 -or
    $emptyTarget.blob_count -ne 0 -or
    $emptyTarget.projection_count -ne 0) {
    throw 'Restore target is not verified empty.'
}

$relativeManifest = [IO.Path]::GetRelativePath($nasRoot, $manifestPath)
$linuxManifest = '/backup/' + ($relativeManifest -replace '\\', '/')
if ($Mode -ceq 'Plan') {
    return [pscustomobject]@{
        Status = 'plan-only'
        MutationPerformed = $false
        BackupId = $backup.backup_id
        TargetDataSpaceId = $deployment.lethe_data_space_id
    }
}

$jobName = "lethe-restore-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
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
                        name = 'restore'
                        image = $deployment.backup_image
                        imagePullPolicy = 'IfNotPresent'
                        args = @(
                            'restore',
                            '--mode',
                            'canonical-event-blob-projection-postgres',
                            '--manifest',
                            $linuxManifest,
                            '--require-empty-target',
                            '--data-space-id',
                            $deployment.lethe_data_space_id
                        )
                        env = @(
                            [ordered]@{
                                name = 'LETHE_HISTORY_BEARER_TOKEN'
                                valueFrom = [ordered]@{
                                    secretKeyRef = [ordered]@{
                                        name = 'lethe-runtime'
                                        key = 'history-bearer-token'
                                    }
                                }
                            },
                            [ordered]@{
                                name = 'BACKUP_ENCRYPTION_KEY'
                                valueFrom = [ordered]@{
                                    secretKeyRef = [ordered]@{
                                        name = 'lethe-runtime'
                                        key = 'backup-encryption-key'
                                    }
                                }
                            },
                            [ordered]@{
                                name = 'POSTGRES_DSN'
                                valueFrom = [ordered]@{
                                    secretKeyRef = [ordered]@{
                                        name = 'lethe-runtime'
                                        key = 'postgres-dsn'
                                    }
                                }
                            }
                        )
                        volumeMounts = @(
                            [ordered]@{
                                name = 'backup'
                                mountPath = '/backup'
                                readOnly = $true
                            }
                        )
                    }
                )
                volumes = @(
                    [ordered]@{
                        name = 'backup'
                        persistentVolumeClaim = [ordered]@{
                            claimName = 'lethe-nas-backup'
                        }
                    }
                )
            }
        }
    }
}
$jobJson = $job | ConvertTo-Json -Depth 32
$jobJson | & kubectl --kubeconfig $LetheKubeconfigPath apply -f -
if ($LASTEXITCODE -ne 0) {
    throw 'Could not create the LETHE restore Job.'
}
& kubectl --kubeconfig $LetheKubeconfigPath wait `
    --namespace lethe-system --for=condition=Complete "job/$jobName" `
    "--timeout=${TimeoutSeconds}s"
if ($LASTEXITCODE -ne 0) {
    throw 'LETHE restore Job did not complete before the deadline.'
}

$lethe = $topology.Clusters | Where-Object Name -CEQ 'lethe'
$state = Invoke-RestMethod -Method Get `
    -Uri "https://$($lethe.Vip)/api/restore/state" `
    -Headers @{ Authorization = "Bearer $($secrets.lethe_history_bearer_token)" }
if ($state.data_space_id -cne $backup.data_space_id -or
    $state.event_cursor -ne $backup.event_cursor -or
    $state.event_count -ne $backup.event_count -or
    $state.blob_count -ne $backup.blob_count -or
    $state.projection_cursor -ne $backup.projection_cursor) {
    throw 'Restored LETHE state differs from the backup receipt.'
}

$receipt = [ordered]@{
    schema_version = 1
    status = 'restore-verified'
    created_at = [DateTimeOffset]::UtcNow.ToString('o')
    backup_id = $backup.backup_id
    data_space_id = $backup.data_space_id
    event_cursor = $backup.event_cursor
    event_count = $backup.event_count
    blob_count = $backup.blob_count
    projection_cursor = $backup.projection_cursor
    backup_receipt_sha256 = Get-FileSha256 -Path $BackupReceiptPath
    empty_target_receipt_sha256 = Get-FileSha256 -Path $EmptyTargetReceiptPath
}
[IO.File]::WriteAllText(
    [IO.Path]::GetFullPath($OutputReceiptPath),
    ($receipt | ConvertTo-Json -Depth 8),
    [Text.UTF8Encoding]::new($false)
)
[pscustomobject]$receipt
