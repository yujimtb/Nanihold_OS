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
    [string] $PreflightReceiptPath,

    [Parameter(Mandatory)]
    [string] $ProvisionReceiptPath,

    [Parameter(Mandatory)]
    [string] $RenderedManifestDirectory,

    [Parameter(Mandatory)]
    [string] $SshPrivateKeyPath,

    [Parameter(Mandatory)]
    [string] $SshPublicKeyPath,

    [Parameter(Mandatory)]
    [string] $KnownHostsPath,

    [Parameter(Mandatory)]
    [string] $KubeconfigOutputDirectory,

    [Parameter(Mandatory)]
    [string] $OutputReceiptPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'HaContract.psm1') -Force

function Invoke-ExactProcess {
    param(
        [Parameter(Mandatory)][string] $FileName,
        [Parameter(Mandatory)][string[]] $Arguments,
        [Parameter()][AllowNull()][string] $StandardInput = $null,
        [Parameter()][switch] $SecretInput
    )
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $FileName
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.RedirectStandardInput = $null -ne $StandardInput
    foreach ($argument in $Arguments) {
        $start.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) {
        throw "Could not start required executable: $FileName"
    }
    if ($null -ne $StandardInput) {
        $process.StandardInput.Write($StandardInput)
        $process.StandardInput.Close()
    }
    $stdout = $process.StandardOutput.ReadToEnd()
    $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    if ($process.ExitCode -ne 0) {
        if ($SecretInput) {
            throw "Secret-bearing process failed: $FileName"
        }
        throw "Process failed with exit $($process.ExitCode): $FileName $stderr"
    }
    return $stdout
}

function Get-SshArguments {
    param([Parameter(Mandatory)][string] $Address)
    return @(
        '-i', [IO.Path]::GetFullPath($SshPrivateKeyPath),
        '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', "UserKnownHostsFile=$([IO.Path]::GetFullPath($KnownHostsPath))",
        '-o', 'ConnectTimeout=10',
        "nanihold@$Address"
    )
}

function Invoke-Ssh {
    param(
        [Parameter(Mandatory)][string] $Address,
        [Parameter(Mandatory)][string[]] $RemoteArguments,
        [Parameter()][AllowNull()][string] $StandardInput = $null,
        [Parameter()][switch] $SecretInput
    )
    $arguments = @(Get-SshArguments -Address $Address) + $RemoteArguments
    return Invoke-ExactProcess -FileName 'ssh' -Arguments $arguments `
        -StandardInput $StandardInput -SecretInput:$SecretInput
}

function Copy-ToNode {
    param(
        [Parameter(Mandatory)][string] $Address,
        [Parameter(Mandatory)][string] $Source,
        [Parameter(Mandatory)][string] $Destination
    )
    $arguments = @(
        '-i', [IO.Path]::GetFullPath($SshPrivateKeyPath),
        '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes',
        '-o', 'StrictHostKeyChecking=yes',
        '-o', "UserKnownHostsFile=$([IO.Path]::GetFullPath($KnownHostsPath))",
        [IO.Path]::GetFullPath($Source),
        "nanihold@${Address}:$Destination"
    )
    $null = Invoke-ExactProcess -FileName 'scp' -Arguments $arguments
}

function ConvertTo-Base64([string] $Value) {
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Value))
}

function New-KubernetesSecretJson {
    param(
        [Parameter(Mandatory)][string] $Namespace,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][hashtable] $Values
    )
    $data = [ordered]@{}
    foreach ($key in $Values.Keys) {
        $data[$key] = ConvertTo-Base64 -Value ([string]$Values[$key])
    }
    return ([ordered]@{
        apiVersion = 'v1'
        kind = 'Secret'
        metadata = [ordered]@{
            namespace = $Namespace
            name = $Name
        }
        type = 'Opaque'
        data = $data
    } | ConvertTo-Json -Depth 16)
}

function Invoke-Kubectl {
    param(
        [Parameter(Mandatory)][string] $Kubeconfig,
        [Parameter(Mandatory)][string[]] $Arguments,
        [Parameter()][AllowNull()][string] $StandardInput = $null,
        [Parameter()][switch] $SecretInput
    )
    $allArguments = @('--kubeconfig', $Kubeconfig) + $Arguments
    return Invoke-ExactProcess -FileName 'kubectl' `
        -Arguments $allArguments `
        -StandardInput $StandardInput -SecretInput:$SecretInput
}

Assert-Administrator
$topology = Read-HaTopology -Path $TopologyPath
$deployment = Read-DeploymentInput -Path $DeploymentInputPath
$secrets = Read-SecretInput -Path $SecretInputPath
$preflight = Read-PreflightReceipt -Path $PreflightReceiptPath `
    -TopologyPath $TopologyPath -DeploymentInputPath $DeploymentInputPath
Assert-NasWritable -Path $preflight.nas_path
Assert-File -Path $SshPrivateKeyPath -Label 'SSH private key'
Assert-PrivateAcl -Path $SshPrivateKeyPath
Assert-File -Path $SshPublicKeyPath -Label 'SSH public key'
if ((Get-FileSha256 -Path $SshPublicKeyPath) -cne
    $preflight.ssh_public_key_sha256) {
    throw 'SSH public key differs from the preflight receipt.'
}
Assert-File -Path $KnownHostsPath -Label 'SSH known_hosts'
Assert-PrivateAcl -Path $KnownHostsPath
foreach ($command in @('ssh', 'scp', 'kubectl')) {
    if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required bootstrap command is unavailable: $command"
    }
}
$derivedPublicKey = (
    Invoke-ExactProcess -FileName 'ssh-keygen' `
        -Arguments @('-y', '-f', [IO.Path]::GetFullPath($SshPrivateKeyPath))
).Trim()
$configuredPublicKey = (
    Get-Content -LiteralPath $SshPublicKeyPath -Raw -Encoding UTF8
).Trim()
$configuredPublicKeyWithoutComment = (
    $configuredPublicKey -split '\s+' | Select-Object -First 2
) -join ' '
if ($derivedPublicKey -cne $configuredPublicKeyWithoutComment) {
    throw 'SSH private key does not match the preflight public key.'
}
if (-not (Test-Path -LiteralPath $RenderedManifestDirectory -PathType Container)) {
    throw "Rendered manifest directory does not exist: $RenderedManifestDirectory"
}
if (-not (Test-Path -LiteralPath $KubeconfigOutputDirectory -PathType Container)) {
    throw "Kubeconfig output directory does not exist: $KubeconfigOutputDirectory"
}
Assert-PrivateAcl -Path $KubeconfigOutputDirectory
if (Test-Path -LiteralPath $OutputReceiptPath) {
    throw "Bootstrap receipt already exists: $OutputReceiptPath"
}

$provision = Get-Content -LiteralPath $ProvisionReceiptPath -Raw -Encoding UTF8 |
    ConvertFrom-Json -Depth 16
Assert-ExactKeys -Value $provision `
    -Keys @(
        'schema_version',
        'status',
        'created_at',
        'host',
        'topology_sha256',
        'preflight_receipt_sha256',
        'nodes'
    ) -Label 'Provision receipt'
if ($provision.schema_version -ne 1 -or
    $provision.status -cne 'provisioned' -or
    $provision.host -cne [Environment]::MachineName -or
    $provision.topology_sha256 -cne (Get-FileSha256 -Path $TopologyPath) -or
    $provision.preflight_receipt_sha256 -cne
    (Get-FileSha256 -Path $PreflightReceiptPath)) {
    throw 'Provision receipt does not match the current host and inputs.'
}
$provisionNodes = @($provision.nodes)
if ($provisionNodes.Count -ne 6) {
    throw 'Provision receipt must contain six VMs.'
}
foreach ($node in $topology.Nodes) {
    $entry = @($provisionNodes | Where-Object name -CEQ $node.Name)
    if ($entry.Count -ne 1) {
        throw "Provision receipt VM mismatch: $($node.Name)"
    }
    $vm = Get-VM -Name $node.Name -ErrorAction Stop
    if ($vm.Id.ToString() -cne $entry[0].vm_id) {
        throw "Provisioned VM identity changed: $($node.Name)"
    }
}

$manifestReceiptPath = Join-Path $RenderedManifestDirectory 'manifest-receipt.json'
$manifestReceipt = Get-Content -LiteralPath $manifestReceiptPath -Raw -Encoding UTF8 |
    ConvertFrom-Json -Depth 16
Assert-ExactKeys -Value $manifestReceipt `
    -Keys @('schema_version', 'topology_sha256', 'deployment_input_sha256', 'files') `
    -Label 'Manifest receipt'
if ($manifestReceipt.schema_version -ne 1 -or
    $manifestReceipt.topology_sha256 -cne (Get-FileSha256 -Path $TopologyPath) -or
    $manifestReceipt.deployment_input_sha256 -cne
    (Get-FileSha256 -Path $DeploymentInputPath)) {
    throw 'Rendered manifest receipt input mismatch.'
}
foreach ($entry in $manifestReceipt.files) {
    Assert-ExactKeys -Value $entry -Keys @('file', 'sha256') `
        -Label 'Rendered manifest receipt entry'
    $path = Join-Path $RenderedManifestDirectory $entry.file
    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($path)) -cne
        [IO.Path]::GetFullPath($RenderedManifestDirectory)) {
        throw 'Rendered manifest must be directly inside its directory.'
    }
    Assert-FileDigest -Path $path -Expected $entry.sha256 `
        -Label "Rendered manifest $($entry.file)"
}

$knownHosts = Get-Content -LiteralPath $KnownHostsPath -Raw -Encoding UTF8
foreach ($node in $topology.Nodes) {
    if ($knownHosts -notmatch [regex]::Escape($node.Address)) {
        throw "known_hosts does not pin $($node.Address)."
    }
}
$naniholdKubeconfig = Join-Path $KubeconfigOutputDirectory 'nanihold.yaml'
$letheKubeconfig = Join-Path $KubeconfigOutputDirectory 'lethe.yaml'
foreach ($path in @($naniholdKubeconfig, $letheKubeconfig)) {
    if (Test-Path -LiteralPath $path) {
        throw "Kubeconfig already exists: $path"
    }
}

if ($Mode -ceq 'Plan') {
    return [pscustomobject]@{
        Status = 'plan-only'
        MutationPerformed = $false
        Clusters = @('nanihold', 'lethe')
        Nodes = @($topology.Nodes).Count
        Nas = $preflight.nas_path
    }
}

foreach ($node in $topology.Nodes) {
    $null = Invoke-Ssh -Address $node.Address -RemoteArguments @('true')
    Copy-ToNode -Address $node.Address -Source $deployment.k3s_binary_path `
        -Destination '/tmp/k3s'
    Copy-ToNode -Address $node.Address `
        -Source $deployment.k3s_install_script_path `
        -Destination '/tmp/install-k3s.sh'
}

foreach ($cluster in $topology.Clusters) {
    $members = @(
        $topology.Nodes |
        Where-Object Cluster -CEQ $cluster.Name |
        Sort-Object Name
    )
    $first = $members[0]
    $token = if ($cluster.Name -ceq 'nanihold') {
        $secrets.nanihold_k3s_token
    }
    else {
        $secrets.lethe_k3s_token
    }
    for ($index = 0; $index -lt $members.Count; $index++) {
        $node = $members[$index]
        $serverArguments = if ($index -eq 0) {
            '--cluster-init'
        }
        else {
            "--server https://$($first.Address):6443"
        }
        $remoteScript = @"
set -euo pipefail
umask 077
test "`$(sha256sum /tmp/k3s | awk '{print `$1}')" = '$($deployment.k3s_binary_sha256)'
test "`$(sha256sum /tmp/install-k3s.sh | awk '{print `$1}')" = '$($deployment.k3s_install_script_sha256)'
install -m 0755 /tmp/k3s /usr/local/bin/k3s
chmod 0700 /tmp/install-k3s.sh
export K3S_TOKEN="`$(printf '%s' '$(ConvertTo-Base64 $token)' | base64 -d)"
export INSTALL_K3S_VERSION='$($deployment.k3s_version)'
export INSTALL_K3S_SKIP_DOWNLOAD=true
export INSTALL_K3S_BIN_DIR=/usr/local/bin
sh /tmp/install-k3s.sh server $serverArguments \
  --node-ip '$($node.Address)' \
  --advertise-address '$($node.Address)' \
  --tls-san '$($cluster.Vip)' \
  --cluster-cidr '$($cluster.PodCidr)' \
  --service-cidr '$($cluster.ServiceCidr)' \
  --disable servicelb \
  --disable traefik \
  --write-kubeconfig-mode 0600
rm -f /tmp/k3s /tmp/install-k3s.sh
"@
        $null = Invoke-Ssh -Address $node.Address `
            -RemoteArguments @('sudo', 'bash', '-s') `
            -StandardInput $remoteScript -SecretInput
    }
    $quorumNode = $members | Where-Object Role -CEQ 'quorum'
    $null = Invoke-Ssh -Address $first.Address `
        -RemoteArguments @(
            'sudo', 'k3s', 'kubectl', 'taint', 'node', $quorumNode.Name,
            'dedicated=quorum:NoSchedule'
        )
}

$letheNodes = @($topology.Nodes | Where-Object Cluster -CEQ 'lethe')
foreach ($node in $letheNodes) {
    Copy-ToNode -Address $node.Address `
        -Source $deployment.backup_image_archive_path `
        -Destination '/tmp/backup-image.tar'
    $imageScript = @"
set -euo pipefail
test "`$(sha256sum /tmp/backup-image.tar | awk '{print `$1}')" = '$($deployment.backup_image_archive_sha256)'
sudo k3s ctr images import /tmp/backup-image.tar >/dev/null
sudo k3s ctr images list -q | grep -F -x '$($deployment.backup_image)' >/dev/null
rm -f /tmp/backup-image.tar
"@
    $null = Invoke-Ssh -Address $node.Address `
        -RemoteArguments @('bash', '-s') -StandardInput $imageScript
}

foreach ($cluster in $topology.Clusters) {
    $first = $topology.Nodes |
        Where-Object Cluster -CEQ $cluster.Name |
        Sort-Object Name |
        Select-Object -First 1
    $content = Invoke-Ssh -Address $first.Address `
        -RemoteArguments @('sudo', 'cat', '/etc/rancher/k3s/k3s.yaml')
    $content = $content.Replace(
        'https://127.0.0.1:6443',
        "https://$($cluster.Vip):6443"
    )
    $path = if ($cluster.Name -ceq 'nanihold') {
        $naniholdKubeconfig
    }
    else {
        $letheKubeconfig
    }
    [IO.File]::WriteAllText(
        $path,
        $content,
        [Text.UTF8Encoding]::new($false)
    )
}

$null = Invoke-Kubectl -Kubeconfig $naniholdKubeconfig `
    -Arguments @('apply', '-f', (Join-Path $RenderedManifestDirectory 'nanihold-kube-vip.yaml'))
$null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
    -Arguments @('apply', '-f', (Join-Path $RenderedManifestDirectory 'lethe-kube-vip.yaml'))
$null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
    -Arguments @('apply', '-f', $deployment.longhorn_manifest_path)
$null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
    -Arguments @(
        'wait', '--for=condition=Ready', 'pod', '--all',
        '--namespace', 'longhorn-system', '--timeout=300s'
    )
$null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
    -Arguments @('apply', '-f', $deployment.cloudnative_pg_manifest_path)
$null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
    -Arguments @(
        'wait', '--for=condition=Established',
        'crd/clusters.postgresql.cnpg.io', '--timeout=300s'
    )
$null = Invoke-Kubectl -Kubeconfig $naniholdKubeconfig `
    -Arguments @('apply', '-f', $deployment.monitoring_manifest_path)
$null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
    -Arguments @('apply', '-f', $deployment.monitoring_manifest_path)
$null = Invoke-Kubectl -Kubeconfig $naniholdKubeconfig `
    -Arguments @(
        'wait', '--for=condition=Established',
        'crd/prometheusrules.monitoring.coreos.com', '--timeout=300s'
    )

$postgresDsn = "postgresql://postgres:$($secrets.postgres_superuser_password)" +
    '@lethe-postgres-rw.lethe-system.svc:5432/lethe?sslmode=require'
$letheSecrets = @()
$letheSecrets += (
    New-KubernetesSecretJson -Namespace 'lethe-system' `
        -Name 'lethe-postgres-superuser' `
        -Values @{
            username = 'postgres'
            password = $secrets.postgres_superuser_password
        }
)
$letheSecrets += (
    New-KubernetesSecretJson -Namespace 'lethe-system' `
        -Name 'lethe-postgres-replication' `
        -Values @{
            username = 'streaming_replica'
            password = $secrets.postgres_replication_password
        }
)
$letheSecrets += (
    New-KubernetesSecretJson -Namespace 'lethe-system' `
        -Name 'lethe-runtime' `
        -Values @{
            'postgres-dsn' = $postgresDsn
            'encryption-key' = $secrets.lethe_encryption_key
            'history-bearer-token' = $secrets.lethe_history_bearer_token
            'backup-encryption-key' = $secrets.backup_encryption_key
        }
)
$null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
    -Arguments @('create', 'namespace', 'lethe-system')
foreach ($secret in $letheSecrets) {
    $null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
        -Arguments @('apply', '-f', '-') -StandardInput $secret -SecretInput
}
$null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
    -Arguments @('apply', '-f', (Join-Path $RenderedManifestDirectory 'lethe.yaml'))
$null = Invoke-Kubectl -Kubeconfig $letheKubeconfig `
    -Arguments @('apply', '-f', (Join-Path $RenderedManifestDirectory 'backup.yaml'))

$naniholdSecrets = @()
$naniholdSecrets += (
    New-KubernetesSecretJson -Namespace 'nanihold-system' `
        -Name 'nanihold-runtime' `
        -Values @{
            'api-bearer-token' = $secrets.nanihold_api_bearer_token
            'lethe-history-bearer-token' = $secrets.lethe_history_bearer_token
        }
)
$naniholdSecrets += (
    New-KubernetesSecretJson -Namespace 'nanihold-system' `
        -Name 'pilot-host-runtime' `
        -Values @{
            PILOT_HOST_BEARER_TOKEN = $secrets.pilot_host_bearer_token
            LETHE_HISTORY_BEARER_TOKEN = $secrets.lethe_history_bearer_token
            MCP_GATEWAY_BEARER_TOKEN = $secrets.mcp_gateway_bearer_token
            ANTHROPIC_API_KEY = $secrets.anthropic_api_key
            OPENAI_API_KEY = $secrets.openai_api_key
        }
)
$null = Invoke-Kubectl -Kubeconfig $naniholdKubeconfig `
    -Arguments @('create', 'namespace', 'nanihold-system')
foreach ($secret in $naniholdSecrets) {
    $null = Invoke-Kubectl -Kubeconfig $naniholdKubeconfig `
        -Arguments @('apply', '-f', '-') -StandardInput $secret -SecretInput
}
$null = Invoke-Kubectl -Kubeconfig $naniholdKubeconfig `
    -Arguments @('apply', '-f', (Join-Path $RenderedManifestDirectory 'nanihold.yaml'))
$null = Invoke-Kubectl -Kubeconfig $naniholdKubeconfig `
    -Arguments @('apply', '-f', (Join-Path $RenderedManifestDirectory 'monitoring-rules.yaml'))

$receipt = [ordered]@{
    schema_version = 1
    status = 'bootstrapped'
    created_at = [DateTimeOffset]::UtcNow.ToString('o')
    host = [Environment]::MachineName
    topology_sha256 = Get-FileSha256 -Path $TopologyPath
    deployment_input_sha256 = Get-FileSha256 -Path $DeploymentInputPath
    provision_receipt_sha256 = Get-FileSha256 -Path $ProvisionReceiptPath
    manifest_receipt_sha256 = Get-FileSha256 -Path $manifestReceiptPath
    nanihold_kubeconfig_sha256 = Get-FileSha256 -Path $naniholdKubeconfig
    lethe_kubeconfig_sha256 = Get-FileSha256 -Path $letheKubeconfig
}
[IO.File]::WriteAllText(
    [IO.Path]::GetFullPath($OutputReceiptPath),
    ($receipt | ConvertTo-Json -Depth 8),
    [Text.UTF8Encoding]::new($false)
)

[pscustomobject]@{
    Status = 'bootstrapped'
    MutationPerformed = $true
    Receipt = [IO.Path]::GetFullPath($OutputReceiptPath)
    SecretValuesLogged = $false
}
