[CmdletBinding()]
param(
    [Parameter()]
    [string] $TopologyPath = (Join-Path $PSScriptRoot 'topology.psd1'),

    [Parameter(Mandatory)]
    [string] $DeploymentInputPath,

    [Parameter(Mandatory)]
    [string] $OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'HaContract.psm1') -Force

$topology = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
if (Test-Path -LiteralPath $OutputDirectory) {
    throw "Manifest output directory already exists: $OutputDirectory"
}
$parent = Split-Path -Parent ([IO.Path]::GetFullPath($OutputDirectory))
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    throw "Manifest output parent does not exist: $parent"
}
$naniholdCluster = $topology.Clusters | Where-Object Name -EQ 'nanihold'
$letheCluster = $topology.Clusters | Where-Object Name -EQ 'lethe'

function Render-Template {
    param(
        [Parameter(Mandatory)][string] $Template,
        [Parameter(Mandatory)][string] $Output,
        [Parameter(Mandatory)][hashtable] $Values
    )
    $content = Get-Content -LiteralPath $Template -Raw -Encoding UTF8
    foreach ($name in $Values.Keys) {
        $content = $content.Replace("@@$name@@", [string]$Values[$name])
    }
    if ($content -match '@@[A-Z0-9_]+@@') {
        throw "Unresolved manifest placeholder in $Template."
    }
    [IO.File]::WriteAllText(
        $Output,
        $content,
        [Text.UTF8Encoding]::new($false)
    )
}

$null = New-Item -ItemType Directory -Path $OutputDirectory
try {
    $templateRoot = Join-Path $PSScriptRoot 'manifests'
    Render-Template `
        -Template (Join-Path $templateRoot 'kube-vip.yaml.tmpl') `
        -Output (Join-Path $OutputDirectory 'nanihold-kube-vip.yaml') `
        -Values @{
            KUBE_VIP_IMAGE = $deployment.kube_vip_image
            CLUSTER_VIP = $naniholdCluster.Vip
        }
    Render-Template `
        -Template (Join-Path $templateRoot 'kube-vip.yaml.tmpl') `
        -Output (Join-Path $OutputDirectory 'lethe-kube-vip.yaml') `
        -Values @{
            KUBE_VIP_IMAGE = $deployment.kube_vip_image
            CLUSTER_VIP = $letheCluster.Vip
        }
    Render-Template `
        -Template (Join-Path $templateRoot 'lethe.yaml.tmpl') `
        -Output (Join-Path $OutputDirectory 'lethe.yaml') `
        -Values @{
            BLOB_STORAGE_GIB = $deployment.blob_storage_gib
            POSTGRES_STORAGE_GIB = $deployment.postgres_storage_gib
            LETHE_IMAGE = $deployment.lethe_image
            LETHE_DATA_SPACE_ID = $deployment.lethe_data_space_id
            LETHE_VIP = $letheCluster.Vip
        }
    Render-Template `
        -Template (Join-Path $templateRoot 'nanihold.yaml.tmpl') `
        -Output (Join-Path $OutputDirectory 'nanihold.yaml') `
        -Values @{
            NANIHOLD_IMAGE = $deployment.nanihold_image
            PILOT_HOST_IMAGE = $deployment.pilot_host_image
            LETHE_DATA_SPACE_ID = $deployment.lethe_data_space_id
            LETHE_VIP = $letheCluster.Vip
            NANIHOLD_VIP = $naniholdCluster.Vip
        }
    Render-Template `
        -Template (Join-Path $templateRoot 'backup.yaml.tmpl') `
        -Output (Join-Path $OutputDirectory 'backup.yaml') `
        -Values @{
            NAS_NFS_SERVER = $deployment.nas_nfs_server
            NAS_NFS_EXPORT = $deployment.nas_nfs_export
            BACKUP_IMAGE = $deployment.backup_image
            LETHE_DATA_SPACE_ID = $deployment.lethe_data_space_id
        }
    Copy-Item -LiteralPath (Join-Path $templateRoot 'monitoring-rules.yaml') `
        -Destination (Join-Path $OutputDirectory 'monitoring-rules.yaml')

    $historyTools = @(
        $deployment.mcp_allowlist |
        Where-Object { $_ -clike 'mcp__history__*' }
    )
    $gatewayTools = @(
        $deployment.mcp_allowlist |
        Where-Object { $_ -clike 'mcp__gateway__*' }
    )
    $allTools = @($historyTools + $gatewayTools)
    $pilotConfigs = @()
    foreach ($pilot in $deployment.pilot_hosts) {
        $config = [ordered]@{
            pilot_host_id = $pilot.pilot_host_id
            device_id = $pilot.device_id
            device_certificate_sha256 = $pilot.device_certificate_sha256
            bearer_token_env = 'PILOT_HOST_BEARER_TOKEN'
            bind_host = '0.0.0.0'
            bind_port = 8444
            receipt_store_path = '/var/lib/nanihold-pilot/receipts.sqlite3'
            claude = [ordered]@{
                candidate = [ordered]@{
                    adapter = 'claude-code'
                    adapter_version = $deployment.claude_cli_version
                    provider = 'anthropic'
                    selection = 'provider_configured'
                    effort = 'high'
                    toolset = $allTools
                    sandbox_fingerprint = "sandbox:$($deployment.pilot_sandbox_certificate_sha256)"
                    environment_fingerprint = $deployment.pilot_host_image
                }
                executable = 'claude'
                cli_version = $deployment.claude_cli_version
                working_directory = '/workspace'
                request_document_directory =
                    '/var/lib/nanihold-pilot/request-documents'
                max_request_document_bytes = 32768
                permission_mode = $deployment.pilot_permission_mode
                sandbox_profile_certificate_sha256 =
                    $deployment.pilot_sandbox_certificate_sha256
                mcp = [ordered]@{
                    allowlist = @('history', 'gateway')
                    servers = [ordered]@{
                        history = [ordered]@{
                            url = "https://$($letheCluster.Vip)/mcp"
                            bearer_token_env_var = 'LETHE_HISTORY_BEARER_TOKEN'
                        }
                        gateway = [ordered]@{
                            url = $deployment.mcp_gateway_url
                            bearer_token_env_var = 'MCP_GATEWAY_BEARER_TOKEN'
                        }
                    }
                }
                max_budget_usd = $deployment.claude_max_budget_usd
                timeout_seconds = $deployment.pilot_timeout_seconds
            }
            codex = [ordered]@{
                candidate = [ordered]@{
                    adapter = 'codex-cli'
                    adapter_version = $deployment.codex_cli_version
                    provider = 'openai'
                    selection = 'exact'
                    model_snapshot = $deployment.codex_model
                    effort = $deployment.codex_effort
                    toolset = $gatewayTools
                    sandbox_fingerprint = 'sandbox:workspace-write'
                    environment_fingerprint = $deployment.pilot_host_image
                }
                executable = 'codex'
                cli_version = $deployment.codex_cli_version
                working_directory_allowlist = @('/workspace')
                sandbox = 'workspace-write'
                mcp = [ordered]@{
                    allowlist = @('gateway')
                    servers = [ordered]@{
                        gateway = [ordered]@{
                            url = $deployment.mcp_gateway_url
                            bearer_token_env_var = 'MCP_GATEWAY_BEARER_TOKEN'
                        }
                    }
                }
                max_input_tokens = $deployment.codex_max_input_tokens
                max_output_tokens = $deployment.codex_max_output_tokens
                max_total_tokens = $deployment.codex_max_total_tokens
                timeout_seconds = $deployment.pilot_timeout_seconds
            }
        }
        $configJson = $config | ConvertTo-Json -Depth 32
        if ($configJson -match '(?i)(api[_-]?key|bearer[_-]?token)"\s*:\s*"[^A-Z_]') {
            throw 'Rendered PilotHost config contains a secret value.'
        }
        $name = if ($pilot.node_name -ceq 'nh-control-a') {
            'pilot-host-a'
        }
        else {
            'pilot-host-b'
        }
        $indented = ($configJson -split "`r?`n" |
            ForEach-Object { "    $_" }) -join [Environment]::NewLine
        $pilotConfigs += @"
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: $name
  namespace: nanihold-system
data:
  config.json: |
$indented
"@
    }
    Add-Content -LiteralPath (Join-Path $OutputDirectory 'nanihold.yaml') `
        -Value ($pilotConfigs -join [Environment]::NewLine) -Encoding UTF8

    $files = @(
        Get-ChildItem -LiteralPath $OutputDirectory -File |
        Sort-Object Name |
        ForEach-Object {
            [ordered]@{
                file = $_.Name
                sha256 = Get-FileSha256 -Path $_.FullName
            }
        }
    )
    $receipt = [ordered]@{
        schema_version = 1
        topology_sha256 = Get-FileSha256 -Path $TopologyPath
        deployment_input_sha256 = Get-FileSha256 -Path $DeploymentInputPath
        files = $files
    }
    [IO.File]::WriteAllText(
        (Join-Path $OutputDirectory 'manifest-receipt.json'),
        ($receipt | ConvertTo-Json -Depth 8),
        [Text.UTF8Encoding]::new($false)
    )
}
catch {
    $resolved = [IO.Path]::GetFullPath($OutputDirectory)
    $resolvedParent = [IO.Path]::GetFullPath($parent)
    if (-not $resolved.StartsWith(
        $resolvedParent + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Refusing manifest cleanup outside the requested parent.'
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
    throw
}

[pscustomobject]@{
    Status = 'rendered'
    Directory = [IO.Path]::GetFullPath($OutputDirectory)
    FileCount = @($files).Count
}
