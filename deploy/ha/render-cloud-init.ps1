[CmdletBinding()]
param(
    [Parameter()]
    [string] $TopologyPath = (Join-Path $PSScriptRoot 'topology.psd1'),

    [Parameter(Mandatory)]
    [string] $DeploymentInputPath,

    [Parameter(Mandatory)]
    [string] $SshPublicKeyPath,

    [Parameter(Mandatory)]
    [string] $OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'HaContract.psm1') -Force

$topology = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
Assert-File -Path $SshPublicKeyPath -Label 'SSH public key'
$sshKey = (Get-Content -LiteralPath $SshPublicKeyPath -Raw -Encoding UTF8).Trim()
if ($sshKey -cnotmatch '^(ssh-ed25519|ssh-rsa) [A-Za-z0-9+/]+={0,3}( .*)?$') {
    throw 'SSH public key is not an accepted OpenSSH public key.'
}
if (Test-Path -LiteralPath $OutputDirectory) {
    throw "Cloud-init output directory already exists: $OutputDirectory"
}
$parent = Split-Path -Parent ([IO.Path]::GetFullPath($OutputDirectory))
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    throw "Cloud-init output parent does not exist: $parent"
}

$created = @()
try {
    $null = New-Item -ItemType Directory -Path $OutputDirectory
    foreach ($node in $topology.Nodes) {
        $nodeDirectory = Join-Path $OutputDirectory $node.Name
        $null = New-Item -ItemType Directory -Path $nodeDirectory
        $created += $nodeDirectory
        $dns = @($topology.DnsServers | ForEach-Object { "`"$_`"" }) -join ', '
        $userData = @"
#cloud-config
autoinstall:
  version: 1
  refresh-installer:
    update: false
  identity:
    hostname: $($node.Name)
    username: nanihold
    password: "!"
  ssh:
    install-server: true
    allow-pw: false
    authorized-keys:
      - "$sshKey"
  storage:
    layout:
      name: lvm
  network:
    version: 2
    ethernets:
      eth0:
        match:
          name: "e*"
        set-name: eth0
        dhcp4: false
        addresses:
          - "$($node.Address)/$($topology.PrefixLength)"
        routes:
          - to: default
            via: "$($topology.Gateway)"
        nameservers:
          addresses: [$dns]
  packages:
    - curl
    - jq
    - nfs-common
    - open-iscsi
    - qemu-guest-agent
  late-commands:
    - curtin in-target -- systemctl enable --now qemu-guest-agent
  shutdown: reboot
"@
        $metaData = @"
instance-id: $($node.Name)-schema2
local-hostname: $($node.Name)
"@
        [IO.File]::WriteAllText(
            (Join-Path $nodeDirectory 'user-data'),
            $userData,
            [Text.UTF8Encoding]::new($false)
        )
        [IO.File]::WriteAllText(
            (Join-Path $nodeDirectory 'meta-data'),
            $metaData,
            [Text.UTF8Encoding]::new($false)
        )
        $seedIso = Join-Path $OutputDirectory "$($node.Name)-seed.iso"
        & $deployment.oscdimg_path -n -m -j1 -lCIDATA $nodeDirectory $seedIso
        if ($LASTEXITCODE -ne 0 -or
            -not (Test-Path -LiteralPath $seedIso -PathType Leaf)) {
            throw "oscdimg failed for $($node.Name)."
        }
    }
    $manifest = [ordered]@{
        schema_version = 1
        topology_sha256 = Get-FileSha256 -Path $TopologyPath
        ssh_public_key_sha256 = Get-FileSha256 -Path $SshPublicKeyPath
        seed_isos = @(
            $topology.Nodes | ForEach-Object {
                $path = Join-Path $OutputDirectory "$($_.Name)-seed.iso"
                [ordered]@{
                    node = $_.Name
                    file = [IO.Path]::GetFileName($path)
                    sha256 = Get-FileSha256 -Path $path
                }
            }
        )
    }
    [IO.File]::WriteAllText(
        (Join-Path $OutputDirectory 'seed-manifest.json'),
        ($manifest | ConvertTo-Json -Depth 8),
        [Text.UTF8Encoding]::new($false)
    )
}
catch {
    if (Test-Path -LiteralPath $OutputDirectory -PathType Container) {
        $resolved = [IO.Path]::GetFullPath($OutputDirectory)
        $resolvedParent = [IO.Path]::GetFullPath($parent)
        if (-not $resolved.StartsWith(
            $resolvedParent + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'Refusing cloud-init cleanup outside the requested parent.'
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    throw
}

[pscustomobject]@{
    Status = 'rendered'
    Directory = [IO.Path]::GetFullPath($OutputDirectory)
    SeedIsoCount = @($topology.Nodes).Count
}
