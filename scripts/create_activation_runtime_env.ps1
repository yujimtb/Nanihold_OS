[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $LetheEnvFile,

    [Parameter(Mandatory = $true)]
    [string] $OutputFile
)

$ErrorActionPreference = "Stop"

function New-HexSecret {
    param([Parameter(Mandatory = $true)][int] $Bytes)

    $buffer = [byte[]]::new($Bytes)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($buffer)
    } finally {
        $generator.Dispose()
    }
    -join ($buffer | ForEach-Object { $_.ToString("x2") })
}

$lethePath = (Resolve-Path -LiteralPath $LetheEnvFile).Path
if (-not (Test-Path -LiteralPath $lethePath -PathType Leaf)) {
    throw "LETHE environment file not found: $lethePath"
}

$outputPath = [IO.Path]::GetFullPath($OutputFile)
if (Test-Path -LiteralPath $outputPath) {
    throw "Activation runtime environment already exists: $outputPath"
}
$outputParent = Split-Path -Parent $outputPath
if (-not (Test-Path -LiteralPath $outputParent -PathType Container)) {
    throw "Activation runtime environment parent does not exist: $outputParent"
}

$matches = @(
    Get-Content -LiteralPath $lethePath |
        Where-Object { $_ -match '^LETHE_NANIHOLD_TOKEN=' }
)
if ($matches.Count -ne 1) {
    throw "LETHE_NANIHOLD_TOKEN must appear exactly once"
}
$letheToken = $matches[0].Split("=", 2)[1]
if (-not $letheToken -or $letheToken -notmatch '^[0-9a-f]{64}$') {
    throw "LETHE_NANIHOLD_TOKEN must be a non-empty 32-byte lowercase hex token"
}

$content = @(
    "LETHE_NANIHOLD_TOKEN=$letheToken"
    "NANIHOLD_API_BEARER_TOKEN=$(New-HexSecret -Bytes 32)"
    "PILOT_HOST_BEARER_TOKEN=$(New-HexSecret -Bytes 32)"
) -join [Environment]::NewLine
$encoding = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText(
    $outputPath,
    $content + [Environment]::NewLine,
    $encoding
)
Write-Output "Created activation runtime environment without printing secrets."
