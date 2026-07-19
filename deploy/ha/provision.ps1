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
    [string] $PreflightReceiptPath,

    [Parameter(Mandatory)]
    [string] $UbuntuIsoPath,

    [Parameter(Mandatory)]
    [string] $SeedIsoDirectory,

    [Parameter(Mandatory)]
    [string] $OutputReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'HaContract.psm1') -Force

Assert-Administrator
$topology = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
$preflight = Read-PreflightReceipt -Path $PreflightReceiptPath `
    -TopologyPath $TopologyPath -DeploymentInputPath $DeploymentInputPath
Assert-FileDigest -Path $UbuntuIsoPath -Expected $deployment.ubuntu_iso_sha256 `
    -Label 'Ubuntu ISO'
if ((Get-FileSha256 -Path $UbuntuIsoPath) -cne $preflight.ubuntu_iso_sha256) {
    throw 'Ubuntu ISO differs from the preflight receipt.'
}
Assert-NasWritable -Path $preflight.nas_path
if (-not (Test-Path -LiteralPath $SeedIsoDirectory -PathType Container)) {
    throw "Seed ISO directory does not exist: $SeedIsoDirectory"
}
$seedManifestPath = Join-Path $SeedIsoDirectory 'seed-manifest.json'
$seedManifest = Get-Content -LiteralPath $seedManifestPath -Raw -Encoding UTF8 |
    ConvertFrom-Json -Depth 16
Assert-ExactKeys -Value $seedManifest `
    -Keys @('schema_version', 'topology_sha256', 'ssh_public_key_sha256', 'seed_isos') `
    -Label 'Seed manifest'
if ($seedManifest.schema_version -ne 1 -or
    $seedManifest.topology_sha256 -cne (Get-FileSha256 -Path $TopologyPath) -or
    $seedManifest.ssh_public_key_sha256 -cne $preflight.ssh_public_key_sha256) {
    throw 'Seed manifest does not match preflight inputs.'
}
$seedEntries = @($seedManifest.seed_isos)
if ($seedEntries.Count -ne @($topology.Nodes).Count) {
    throw 'Seed manifest must contain exactly one ISO per VM.'
}
$seedByNode = @{}
foreach ($entry in $seedEntries) {
    Assert-ExactKeys -Value $entry -Keys @('node', 'file', 'sha256') `
        -Label 'Seed manifest entry'
    if ($seedByNode.ContainsKey($entry.node)) {
        throw "Duplicate seed ISO node: $($entry.node)"
    }
    $seedPath = Join-Path $SeedIsoDirectory $entry.file
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($seedPath)) -cne
        [IO.Path]::GetFullPath($SeedIsoDirectory)) {
        throw 'Seed ISO must be directly inside SeedIsoDirectory.'
    }
    Assert-FileDigest -Path $seedPath -Expected $entry.sha256 `
        -Label "Seed ISO $($entry.node)"
    $seedByNode[$entry.node] = [IO.Path]::GetFullPath($seedPath)
}
foreach ($node in $topology.Nodes) {
    if (-not $seedByNode.ContainsKey($node.Name)) {
        throw "Seed ISO is missing for $($node.Name)."
    }
    if (Get-VM -Name $node.Name -ErrorAction SilentlyContinue) {
        throw "VM already exists: $($node.Name)"
    }
    if (Test-Path -LiteralPath (Join-Path $topology.VmRoot $node.Name)) {
        throw "VM directory already exists: $($node.Name)"
    }
}
if (Test-Path -LiteralPath $OutputReceiptPath) {
    throw "Provision receipt already exists: $OutputReceiptPath"
}
$receiptParent = Split-Path -Parent ([IO.Path]::GetFullPath($OutputReceiptPath))
if (-not (Test-Path -LiteralPath $receiptParent -PathType Container)) {
    throw "Provision receipt directory does not exist: $receiptParent"
}

$plan = @(
    $topology.Nodes | ForEach-Object {
        [pscustomobject]@{
            Name = $_.Name
            Cluster = $_.Cluster
            Address = $_.Address
            Cpu = $_.Cpu
            StartupMemoryGiB = $_.StartupMemoryGiB
            DiskGiB = $_.DiskGiB
            Switch = $topology.SwitchName
            SeedIso = $seedByNode[$_.Name]
        }
    }
)
if ($Mode -ceq 'Plan') {
    return [pscustomobject]@{
        Status = 'plan-only'
        MutationPerformed = $false
        Nodes = $plan
    }
}

$createdNames = [Collections.Generic.List[string]]::new()
try {
    foreach ($node in $topology.Nodes) {
        $vmPath = Join-Path $topology.VmRoot $node.Name
        $vhdPath = Join-Path $vmPath "$($node.Name).vhdx"
        $vm = New-VM -Name $node.Name -Generation 2 `
            -Path $vmPath `
            -MemoryStartupBytes ($node.StartupMemoryGiB * 1GB) `
            -NewVHDPath $vhdPath `
            -NewVHDSizeBytes ($node.DiskGiB * 1GB) `
            -SwitchName $topology.SwitchName
        $createdNames.Add($node.Name)
        Set-VMProcessor -VM $vm -Count $node.Cpu
        Set-VMMemory -VM $vm -DynamicMemoryEnabled $true `
            -MinimumBytes ($node.MinimumMemoryGiB * 1GB) `
            -StartupBytes ($node.StartupMemoryGiB * 1GB) `
            -MaximumBytes ($node.MaximumMemoryGiB * 1GB)
        Set-VMFirmware -VM $vm -EnableSecureBoot On `
            -SecureBootTemplate 'MicrosoftUEFICertificateAuthority'
        $ubuntuDvd = Add-VMDvdDrive -VM $vm -Path $UbuntuIsoPath -Passthru
        $null = Add-VMDvdDrive -VM $vm -Path $seedByNode[$node.Name] -Passthru
        Set-VMFirmware -VM $vm -FirstBootDevice $ubuntuDvd
        Set-VM -VM $vm -AutomaticStartAction Nothing `
            -AutomaticStopAction ShutDown -CheckpointType Disabled
    }
    foreach ($name in $createdNames) {
        Start-VM -Name $name
    }
}
catch {
    $createdInReverse = $createdNames.ToArray()
    [array]::Reverse($createdInReverse)
    foreach ($name in $createdInReverse) {
        $vm = Get-VM -Name $name -ErrorAction SilentlyContinue
        if ($null -ne $vm) {
            Stop-VM -VM $vm -TurnOff -Force -ErrorAction SilentlyContinue
            Remove-VM -VM $vm -Force -ErrorAction SilentlyContinue
        }
        $vmPath = [IO.Path]::GetFullPath((Join-Path $topology.VmRoot $name))
        $root = [IO.Path]::GetFullPath($topology.VmRoot)
        if (-not $vmPath.StartsWith(
            $root + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'Refusing rollback outside the configured VM root.'
        }
        if (Test-Path -LiteralPath $vmPath -PathType Container) {
            Remove-Item -LiteralPath $vmPath -Recurse -Force
        }
    }
    throw
}

$receipt = [ordered]@{
    schema_version = 1
    status = 'provisioned'
    created_at = [DateTimeOffset]::UtcNow.ToString('o')
    host = [Environment]::MachineName
    topology_sha256 = Get-FileSha256 -Path $TopologyPath
    preflight_receipt_sha256 = Get-FileSha256 -Path $PreflightReceiptPath
    nodes = @(
        $topology.Nodes | ForEach-Object {
            $vm = Get-VM -Name $_.Name -ErrorAction Stop
            [ordered]@{
                name = $_.Name
                vm_id = $vm.Id.ToString()
                seed_iso_sha256 = Get-FileSha256 -Path $seedByNode[$_.Name]
            }
        }
    )
}
[IO.File]::WriteAllText(
    [IO.Path]::GetFullPath($OutputReceiptPath),
    ($receipt | ConvertTo-Json -Depth 8),
    [Text.UTF8Encoding]::new($false)
)

[pscustomobject]@{
    Status = 'provisioned'
    MutationPerformed = $true
    Receipt = [IO.Path]::GetFullPath($OutputReceiptPath)
    Nodes = @($topology.Nodes).Count
}
