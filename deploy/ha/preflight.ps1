[CmdletBinding()]
param(
    [Parameter()]
    [string] $TopologyPath = (Join-Path $PSScriptRoot 'topology.psd1'),

    [Parameter(Mandatory)]
    [string] $DeploymentInputPath,

    [Parameter(Mandatory)]
    [string] $SecretInputPath,

    [Parameter(Mandatory)]
    [string] $UbuntuIsoPath,

    [Parameter(Mandatory)]
    [string] $SshPublicKeyPath,

    [Parameter(Mandatory)]
    [string] $NasBackupPath,

    [Parameter(Mandatory)]
    [string] $OutputReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'HaContract.psm1') -Force

Assert-Administrator
$topology = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
$null = Read-SecretInput -Path $SecretInputPath
Assert-FileDigest -Path $UbuntuIsoPath -Expected $deployment.ubuntu_iso_sha256 `
    -Label 'Ubuntu ISO'
Assert-File -Path $SshPublicKeyPath -Label 'SSH public key'
if ([IO.Path]::GetExtension($UbuntuIsoPath) -cne '.iso') {
    throw 'Ubuntu installation media must be an ISO file.'
}
$sshKey = (Get-Content -LiteralPath $SshPublicKeyPath -Raw -Encoding UTF8).Trim()
if ($sshKey -cnotmatch '^(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/]+={0,3}( .*)?$') {
    throw 'SSH public key is not an accepted OpenSSH public key.'
}
if (Test-Path -LiteralPath $OutputReceiptPath) {
    throw "Preflight receipt already exists: $OutputReceiptPath"
}
$receiptParent = Split-Path -Parent ([IO.Path]::GetFullPath($OutputReceiptPath))
if (-not (Test-Path -LiteralPath $receiptParent -PathType Container)) {
    throw "Preflight receipt directory does not exist: $receiptParent"
}

foreach ($command in @(
    'Get-VM',
    'Get-VMSwitch',
    'Get-VMHost',
    'Get-NetIPAddress',
    'Get-NetNeighbor',
    'Test-NetConnection'
)) {
    if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required Hyper-V/network command is unavailable: $command"
    }
}

$switch = Get-VMSwitch -Name $topology.SwitchName -ErrorAction Stop
if ($switch.SwitchType -ne 'Internal') {
    throw "Hyper-V switch must be Internal: $($topology.SwitchName)"
}
$gateway = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $topology.Gateway `
    -ErrorAction SilentlyContinue
if ($null -eq $gateway) {
    throw "Host gateway address is not configured: $($topology.Gateway)"
}

$mcpUri = [Uri]$deployment.mcp_gateway_url
$mcpPort = if ($mcpUri.IsDefaultPort) { 443 } else { $mcpUri.Port }
$mcpProbe = Test-NetConnection -ComputerName $topology.ExistingMcpGateway `
    -Port $mcpPort -WarningAction SilentlyContinue
if (-not $mcpProbe.TcpTestSucceeded) {
    throw "Existing MCP Gateway is unreachable: $($topology.ExistingMcpGateway):$mcpPort"
}

$plannedAddresses = @($topology.Nodes.Address) +
    @($topology.Clusters.Vip)
foreach ($node in $topology.Nodes) {
    if (Get-VM -Name $node.Name -ErrorAction SilentlyContinue) {
        throw "VM already exists: $($node.Name)"
    }
    $vmPath = Join-Path $topology.VmRoot $node.Name
    if (Test-Path -LiteralPath $vmPath) {
        throw "VM directory already exists: $vmPath"
    }
}
foreach ($address in $plannedAddresses) {
    $null = Test-Connection -TargetName $address -Count 1 -Quiet `
        -ErrorAction SilentlyContinue
    $neighbor = Get-NetNeighbor -AddressFamily IPv4 -IPAddress $address `
        -ErrorAction SilentlyContinue |
        Where-Object State -NotIn @('Unreachable', 'Incomplete')
    if ($null -ne $neighbor) {
        throw "Planned address is already present in the neighbor table: $address"
    }
}

if (-not (Test-Path -LiteralPath $topology.VmRoot -PathType Container)) {
    throw "VM root does not exist: $($topology.VmRoot)"
}
$requiredMemoryGiB = [int](($topology.Nodes |
    Measure-Object StartupMemoryGiB -Sum).Sum)
$requiredDiskGiB = [int](($topology.Nodes |
    Measure-Object DiskGiB -Sum).Sum)
$hostMemory = Get-CimInstance Win32_OperatingSystem
$freeMemoryBytes = [uint64]$hostMemory.FreePhysicalMemory * 1KB
if ($freeMemoryBytes -lt ($requiredMemoryGiB * 1GB)) {
    throw "Insufficient currently free memory: need ${requiredMemoryGiB}GiB."
}
$driveName = ([IO.Path]::GetPathRoot($topology.VmRoot)).TrimEnd(':\')
$drive = Get-PSDrive -Name $driveName -ErrorAction Stop
if ($drive.Free -lt ($requiredDiskGiB * 1GB)) {
    throw "Insufficient free space under $($topology.VmRoot): need ${requiredDiskGiB}GiB."
}

Assert-NasWritable -Path $NasBackupPath

$receipt = [ordered]@{
    schema_version = 1
    status = 'ready'
    created_at = [DateTimeOffset]::UtcNow.ToString('o')
    host = [Environment]::MachineName
    topology_sha256 = Get-FileSha256 -Path $TopologyPath
    deployment_input_sha256 = Get-FileSha256 -Path $DeploymentInputPath
    ubuntu_iso_sha256 = Get-FileSha256 -Path $UbuntuIsoPath
    ssh_public_key_sha256 = Get-FileSha256 -Path $SshPublicKeyPath
    nas_path = [IO.Path]::GetFullPath($NasBackupPath)
    switch_name = $topology.SwitchName
    node_count = @($topology.Nodes).Count
    nanihold_vip = ($topology.Clusters | Where-Object Name -EQ 'nanihold').Vip
    lethe_vip = ($topology.Clusters | Where-Object Name -EQ 'lethe').Vip
    required_memory_gib = $requiredMemoryGiB
    required_disk_gib = $requiredDiskGiB
}
$encoded = $receipt | ConvertTo-Json -Depth 8
$stream = [IO.File]::Open(
    [IO.Path]::GetFullPath($OutputReceiptPath),
    [IO.FileMode]::CreateNew,
    [IO.FileAccess]::Write,
    [IO.FileShare]::None
)
try {
    $writer = [IO.StreamWriter]::new($stream, [Text.UTF8Encoding]::new($false))
    try {
        $writer.Write($encoded)
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

[pscustomobject]@{
    Status = 'ready'
    Receipt = [IO.Path]::GetFullPath($OutputReceiptPath)
    Nodes = @($topology.Nodes).Count
    NaniholdVip = $receipt.nanihold_vip
    LetheVip = $receipt.lethe_vip
    Nas = $receipt.nas_path
}
