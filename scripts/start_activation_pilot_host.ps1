[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory = $true)]
    [string] $PythonExecutable,

    [Parameter(Mandatory = $true)]
    [string] $RepositoryRoot,

    [Parameter(Mandatory = $true)]
    [string] $ConfigFile,

    [Parameter(Mandatory = $true)]
    [string] $RuntimeEnvFile,

    [Parameter(Mandatory = $true)]
    [string] $LogFile,

    [Parameter(Mandatory = $true)]
    [string] $PidFile,

    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 300)]
    [int] $ReadyTimeoutSeconds
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Resolve-RequiredFile {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $Label
    )
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "$Label not found: $resolved"
    }
    $resolved
}

function Get-RequiredConfigString {
    param(
        [Parameter(Mandatory = $true)][psobject] $Config,
        [Parameter(Mandatory = $true)][string] $Name
    )
    if (
        $Name -notin $Config.PSObject.Properties.Name -or
        $Config.$Name -isnot [string] -or
        -not $Config.$Name.Trim()
    ) {
        throw "PilotHost configuration field must be a non-blank string: $Name"
    }
    $Config.$Name
}

function Get-StreamEvidence {
    param([Parameter(Mandatory = $true)][string] $Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return "stderr_bytes=0;stderr_sha256=none"
    }
    $bytes = [IO.File]::ReadAllBytes($Path)
    $digest = [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData($bytes)
    ).ToLowerInvariant()
    "stderr_bytes=$($bytes.Length);stderr_sha256=$digest"
}

function Stop-StartedProcess {
    param([Diagnostics.Process] $Process)

    if ($null -eq $Process) {
        return
    }
    try {
        $Process.Refresh()
        if (-not $Process.HasExited) {
            Stop-Process -Id $Process.Id -Force -ErrorAction Stop
            $Process.WaitForExit(5000) | Out-Null
        }
    } catch {
        # The original startup error remains authoritative.
    }
}

$python = Resolve-RequiredFile $PythonExecutable "Python executable"
$repository = (Resolve-Path -LiteralPath $RepositoryRoot).Path
if (-not (Test-Path -LiteralPath $repository -PathType Container)) {
    throw "Repository root not found: $repository"
}
$config = Resolve-RequiredFile $ConfigFile "PilotHost configuration"
$runtimeEnv = Resolve-RequiredFile $RuntimeEnvFile "Activation runtime environment"
$logPath = [IO.Path]::GetFullPath($LogFile)
$pidPath = [IO.Path]::GetFullPath($PidFile)
$stdoutPath = "$logPath.stdout"
$stderrPath = "$logPath.stderr"

foreach ($path in @($logPath, $pidPath, $stdoutPath, $stderrPath)) {
    $parent = Split-Path -Parent $path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw "PilotHost output parent does not exist: $parent"
    }
}
if (Test-Path -LiteralPath $pidPath) {
    throw "PilotHost PID receipt already exists: $pidPath"
}

$configDocument = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json -Depth 100
$pilotHostId = Get-RequiredConfigString $configDocument "pilot_host_id"
$deviceId = Get-RequiredConfigString $configDocument "device_id"
$certificateSha256 = Get-RequiredConfigString `
    $configDocument `
    "device_certificate_sha256"
$bearerTokenEnv = Get-RequiredConfigString $configDocument "bearer_token_env"
$bindHost = Get-RequiredConfigString $configDocument "bind_host"
if ($certificateSha256 -notmatch "^[0-9a-f]{64}$") {
    throw "PilotHost device certificate SHA-256 has invalid format"
}
if ($bearerTokenEnv -ne "PILOT_HOST_BEARER_TOKEN") {
    throw "PilotHost bearer_token_env must be PILOT_HOST_BEARER_TOKEN"
}
if ($bindHost -notin @("127.0.0.1", "::1", "localhost")) {
    throw "Activation PilotHost launcher requires a loopback bind_host"
}
if (
    "bind_port" -notin $configDocument.PSObject.Properties.Name -or
    $configDocument.bind_port -isnot [long] -or
    $configDocument.bind_port -lt 1 -or
    $configDocument.bind_port -gt 65535
) {
    throw "PilotHost bind_port must be an integer from 1 through 65535"
}
$healthUri = [UriBuilder]::new(
    "http",
    $bindHost,
    [int] $configDocument.bind_port,
    "health"
).Uri.AbsoluteUri

$entries = @{}
foreach ($line in Get-Content -LiteralPath $runtimeEnv) {
    if (-not $line -or $line.StartsWith("#")) {
        continue
    }
    $parts = $line.Split("=", 2)
    if ($parts.Count -ne 2 -or -not $parts[0] -or -not $parts[1]) {
        throw "Invalid activation runtime environment entry"
    }
    if ($entries.ContainsKey($parts[0])) {
        throw "Duplicate activation runtime environment key: $($parts[0])"
    }
    $entries[$parts[0]] = $parts[1]
}
$expected = @(
    "LETHE_NANIHOLD_TOKEN",
    "NANIHOLD_API_BEARER_TOKEN",
    "PILOT_HOST_BEARER_TOKEN"
)
if (
    @($entries.Keys | Where-Object { $_ -notin $expected }).Count -ne 0 -or
    @($expected | Where-Object { -not $entries.ContainsKey($_) }).Count -ne 0
) {
    throw "Activation runtime environment must contain exactly three required keys"
}
foreach ($name in $expected) {
    if ($entries[$name] -notmatch "^[0-9a-f]{64}$") {
        throw "Activation runtime environment value has invalid format: $name"
    }
}

$originalEnvironment = @{}
foreach ($name in $expected) {
    $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable(
        $name,
        [EnvironmentVariableTarget]::Process
    )
}
$process = $null
$ready = $false
try {
    try {
        foreach ($name in $expected) {
            [Environment]::SetEnvironmentVariable(
                $name,
                $entries[$name],
                [EnvironmentVariableTarget]::Process
            )
        }
        # Start-Process -Environment rewrites PATH on Windows. Inherit the exact
        # parent environment and overlay the three secrets only for process creation.
        $process = Start-Process `
            -FilePath $python `
            -ArgumentList @(
                "-m",
                "scripts.production_pilot_host",
                "--config",
                $config,
                "--log-file",
                $logPath
            ) `
            -WorkingDirectory $repository `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -PassThru
    } finally {
        foreach ($name in $expected) {
            [Environment]::SetEnvironmentVariable(
                $name,
                $originalEnvironment[$name],
                [EnvironmentVariableTarget]::Process
            )
        }
    }

    $headers = @{
        "Authorization" = "Bearer $($entries["PILOT_HOST_BEARER_TOKEN"])"
        "X-Nanihold-Pilot-Host-Id" = $pilotHostId
        "X-Nanihold-Device-Id" = $deviceId
        "X-Nanihold-Device-Certificate-Sha256" = $certificateSha256
    }
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($ReadyTimeoutSeconds)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        $process.Refresh()
        if ($process.HasExited) {
            $stderrEvidence = Get-StreamEvidence $stderrPath
            throw (
                "PilotHost exited before ready; exit_code=$($process.ExitCode);" +
                $stderrEvidence
            )
        }

        $health = $null
        try {
            $health = Invoke-RestMethod `
                -Method Get `
                -Uri $healthUri `
                -Headers $headers `
                -NoProxy `
                -TimeoutSec 2
        } catch {
            if ($null -ne $_.Exception.Response) {
                throw "PilotHost health endpoint rejected the authenticated probe"
            }
        }
        if ($null -ne $health) {
            if (
                $health.status -ne "ready" -or
                $health.identity.pilot_host_id -ne $pilotHostId -or
                $health.identity.device_id -ne $deviceId -or
                $health.identity.certificate_sha256 -ne $certificateSha256
            ) {
                throw "PilotHost health response violates the configured identity"
            }
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) {
        throw "PilotHost did not report ready before the startup deadline"
    }

    $pidBytes = [Text.UTF8Encoding]::new($false).GetBytes(
        "$($process.Id)$([Environment]::NewLine)"
    )
    $pidStream = [IO.FileStream]::new(
        $pidPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $pidStream.Write($pidBytes, 0, $pidBytes.Length)
        $pidStream.Flush($true)
    } finally {
        $pidStream.Dispose()
    }
} catch {
    Stop-StartedProcess $process
    throw
}

Write-Output "PilotHost ready; PID receipt written."
