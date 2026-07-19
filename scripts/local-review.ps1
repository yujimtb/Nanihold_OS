[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "init", "up", "status", "smoke", "token", "logs", "down")]
    [string] $Command = "help"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$StateRoot = Join-Path $ProjectRoot ".local-verification"
$RuntimeEnv = Join-Path $StateRoot "runtime.env"
$ComposeEnv = Join-Path $StateRoot "compose.env"
$NaniholdConfig = Join-Path $StateRoot "vsm.toml"
$PilotConfig = Join-Path $StateRoot "pilot-host.json"
$PilotWorkspace = Join-Path $StateRoot "pilot-workspace"
$PilotPidPath = Join-Path $StateRoot "pilot-host.pid"
$PilotLog = Join-Path $StateRoot "pilot-host.log"
$LetheSource = Join-Path (Split-Path -Parent $ProjectRoot) "skcollege_database"
$ComposeProject = "nanihold-local-review"

function New-HexSecret {
    param([Parameter(Mandatory = $true)][int] $Bytes)
    $buffer = [byte[]]::new($Bytes)
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($buffer)
    } finally {
        $generator.Dispose()
    }
    return ([BitConverter]::ToString($buffer) -replace "-", "").ToLowerInvariant()
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string] $Path,
        [Parameter(Mandatory = $true)][string] $Content
    )
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $encoding)
}

function New-FreeTcpPort {
    $listener = New-Object Net.Sockets.TcpListener(
        [Net.IPAddress]::Loopback,
        0
    )
    try {
        $listener.Start()
        return ([Net.IPEndPoint] $listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

function Get-EnvMap {
    if (-not (Test-Path -LiteralPath $RuntimeEnv -PathType Leaf)) {
        throw "Local verification is not initialized. Run .\local-review.cmd init."
    }
    $result = @{}
    foreach ($line in Get-Content -LiteralPath $RuntimeEnv) {
        if (-not $line.Trim()) {
            continue
        }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2 -or -not $parts[0] -or -not $parts[1]) {
            throw "Invalid runtime.env entry."
        }
        if ($result.ContainsKey($parts[0])) {
            throw "Duplicate runtime.env key: $($parts[0])"
        }
        $result[$parts[0]] = $parts[1]
    }
    return $result
}

function Get-ComposeMap {
    if (-not (Test-Path -LiteralPath $ComposeEnv -PathType Leaf)) {
        throw "Local verification compose.env is missing."
    }
    $result = @{}
    foreach ($line in Get-Content -LiteralPath $ComposeEnv) {
        if (-not $line.Trim()) {
            continue
        }
        $parts = $line.Split("=", 2)
        if ($parts.Count -ne 2 -or -not $parts[0] -or -not $parts[1]) {
            throw "Invalid compose.env entry."
        }
        if ($result.ContainsKey($parts[0])) {
            throw "Duplicate compose.env key: $($parts[0])"
        }
        $result[$parts[0]] = $parts[1]
    }
    return $result
}

function Get-DockerPath {
    param([Parameter(Mandatory = $true)][string] $WindowsPath)
    $resolved = (Resolve-Path -LiteralPath $WindowsPath).Path
    return $resolved.Replace("\", "/")
}

function Invoke-Compose {
    param([Parameter(Mandatory = $true)][string[]] $Arguments)
    Push-Location $ProjectRoot
    try {
        & docker compose `
            --project-name $ComposeProject `
            --env-file .local-verification/compose.env `
            -f compose.yaml `
            -f compose.local.yaml `
            --profile local `
            --profile runtime `
            @Arguments
    } finally {
        Pop-Location
    }
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit $LASTEXITCODE"
    }
}

function Wait-Http {
    param(
        [Parameter(Mandatory = $true)][string] $Uri,
        [Parameter(Mandatory = $true)][string] $BearerToken,
        [Parameter(Mandatory = $true)][string] $Name
    )
    $headers = @{ Authorization = "Bearer $BearerToken" }
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -Headers $headers -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                return
            }
        } catch {
            if ($attempt -eq 60) {
                throw "$Name did not become ready: $($_.Exception.Message)"
            }
        }
        Start-Sleep -Milliseconds 500
    }
}

function Stop-PilotHost {
    if (-not (Test-Path -LiteralPath $PilotPidPath -PathType Leaf)) {
        return
    }
    $pidText = (Get-Content -LiteralPath $PilotPidPath -Raw).Trim()
    $pidValue = 0
    if (-not [int]::TryParse($pidText, [ref] $pidValue)) {
        throw "Invalid PilotHost PID file."
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        $expectedScript = (Join-Path $PSScriptRoot "local_pilot_host.py")
        if (
            $process.Name -notmatch "^python" -or
            $process.CommandLine -notlike "*$expectedScript*"
        ) {
            throw "PID $pidValue is not the Nanihold local PilotHost."
        }
        Stop-Process -Id $pidValue
    }
    Remove-Item -LiteralPath $PilotPidPath
}

function Start-PilotHost {
    if (Test-Path -LiteralPath $PilotPidPath -PathType Leaf) {
        $pidText = (Get-Content -LiteralPath $PilotPidPath -Raw).Trim()
        $existing = Get-Process -Id ([int] $pidText) -ErrorAction SilentlyContinue
        if ($null -ne $existing) {
            throw "PilotHost is already running with PID $pidText."
        }
        Remove-Item -LiteralPath $PilotPidPath
    }
    $envMap = Get-EnvMap
    $composeMap = Get-ComposeMap
    $python = (Get-Command python -ErrorAction Stop).Source
    $pilotScript = Join-Path $PSScriptRoot "local_pilot_host.py"
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    $startInfo.Arguments = (
        "`"$pilotScript`" --config `"$PilotConfig`" " +
        "--working-directory `"$PilotWorkspace`" --log-file `"$PilotLog`""
    )
    $startInfo.WorkingDirectory = $ProjectRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.EnvironmentVariables["PILOT_HOST_BEARER_TOKEN"] = (
        $envMap["PILOT_HOST_BEARER_TOKEN"]
    )
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Could not start the local PilotHost."
    }
    Set-Content -LiteralPath $PilotPidPath -Value $process.Id -Encoding ascii
    try {
        Wait-Http `
            -Uri "http://127.0.0.1:$($composeMap["PILOT_HOST_PORT"])/health" `
            -BearerToken $envMap["PILOT_HOST_BEARER_TOKEN"] `
            -Name "PilotHost"
    } catch {
        Stop-PilotHost
        throw
    }
}

function Initialize-LocalReview {
    if (Test-Path -LiteralPath $StateRoot) {
        throw ".local-verification already exists; refusing to overwrite it."
    }
    if (-not (Test-Path -LiteralPath $LetheSource -PathType Container)) {
        throw "Required sibling LETHE repository not found: $LetheSource"
    }
    $null = Get-Command docker -ErrorAction Stop
    $npmRoot = Split-Path -Parent (Get-Command claude.cmd -ErrorAction Stop).Source
    $claudeExecutable = Join-Path $npmRoot "node_modules\@anthropic-ai\claude-code\bin\claude.exe"
    if (-not (Test-Path -LiteralPath $claudeExecutable -PathType Leaf)) {
        throw "Claude Code native executable not found: $claudeExecutable"
    }
    $claudeVersionText = (& $claudeExecutable --version).Trim()
    if ($LASTEXITCODE -ne 0 -or $claudeVersionText -notmatch "^([0-9]+\.[0-9]+\.[0-9]+) \(Claude Code\)$") {
        throw "Could not determine the exact Claude Code version."
    }
    $claudeVersion = $Matches[1]
    $fingerprintSource = "$claudeVersion|windows-pilot-host|claude-haiku-4-5-20251001|low|tools-disabled"
    $fingerprintBytes = [Text.Encoding]::UTF8.GetBytes($fingerprintSource)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $fingerprintHash = $sha256.ComputeHash($fingerprintBytes)
    } finally {
        $sha256.Dispose()
    }
    $environmentFingerprint = "sha256:" + (
        ($fingerprintHash | ForEach-Object { $_.ToString("x2") }) -join ""
    )
    $allocatedPorts = @{}
    foreach ($name in @(
        "PILOT_HOST_PORT",
        "LETHE_HTTP_HOST_PORT",
        "LETHE_MCP_HOST_PORT",
        "NANIHOLD_API_HOST_PORT",
        "NANIHOLD_WEB_HOST_PORT"
    )) {
        do {
            $port = New-FreeTcpPort
        } while ($allocatedPorts.Values -contains $port)
        $allocatedPorts[$name] = $port
    }

    New-Item -ItemType Directory -Path $StateRoot | Out-Null
    New-Item -ItemType Directory -Path $PilotWorkspace | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $StateRoot "lethe-data") | Out-Null

    $naniholdTemplate = Get-Content `
        -LiteralPath (Join-Path $ProjectRoot "config\nanihold.local.toml.template") `
        -Raw
    $naniholdRendered = $naniholdTemplate.Replace(
        "@@CLAUDE_VERSION@@",
        $claudeVersion
    ).Replace(
        "@@ENVIRONMENT_FINGERPRINT@@",
        $environmentFingerprint
    ).Replace(
        "@@PILOT_HOST_PORT@@",
        [string] $allocatedPorts["PILOT_HOST_PORT"]
    ).Replace(
        "@@NANIHOLD_WEB_HOST_PORT@@",
        [string] $allocatedPorts["NANIHOLD_WEB_HOST_PORT"]
    )
    Write-Utf8NoBom -Path $NaniholdConfig -Content $naniholdRendered

    $pilotTemplate = Get-Content `
        -LiteralPath (Join-Path $ProjectRoot "config\pilot-host.local.json.template") `
        -Raw
    $pilotRendered = $pilotTemplate.Replace(
        "@@CLAUDE_VERSION@@",
        $claudeVersion
    ).Replace(
        "@@ENVIRONMENT_FINGERPRINT@@",
        $environmentFingerprint
    ).Replace(
        "@@CLAUDE_EXECUTABLE_JSON@@",
        ($claudeExecutable | ConvertTo-Json -Compress)
    ).Replace(
        "@@PILOT_HOST_PORT@@",
        [string] $allocatedPorts["PILOT_HOST_PORT"]
    )
    Write-Utf8NoBom -Path $PilotConfig -Content $pilotRendered

    $runtimeEnvironment = @(
        "LETHE_STORAGE_ENCRYPTION_KEY=$(New-HexSecret 32)",
        "LETHE_OPERATIONAL_STORAGE_ENCRYPTION_KEY=$(New-HexSecret 32)",
        "LETHE_NANIHOLD_TOKEN=$(New-HexSecret 32)",
        "LETHE_API_READ_TOKEN=$(New-HexSecret 32)",
        "LETHE_API_WRITE_TOKEN=$(New-HexSecret 32)",
        "LETHE_API_SYNC_TOKEN=$(New-HexSecret 32)",
        "NANIHOLD_API_BEARER_TOKEN=$(New-HexSecret 32)",
        "PILOT_HOST_BEARER_TOKEN=$(New-HexSecret 32)"
    ) -join "`n"
    Write-Utf8NoBom -Path $RuntimeEnv -Content "$runtimeEnvironment`n"

    $letheDockerPath = Get-DockerPath $LetheSource
    $composeEnvironment = @(
        "NANIHOLD_ENV_FILE=.local-verification/runtime.env",
        "LETHE_SOURCE_DIR=$letheDockerPath",
        "PILOT_HOST_PORT=$($allocatedPorts["PILOT_HOST_PORT"])",
        "LETHE_HTTP_HOST_PORT=$($allocatedPorts["LETHE_HTTP_HOST_PORT"])",
        "LETHE_MCP_HOST_PORT=$($allocatedPorts["LETHE_MCP_HOST_PORT"])",
        "NANIHOLD_API_HOST_PORT=$($allocatedPorts["NANIHOLD_API_HOST_PORT"])",
        "NANIHOLD_WEB_HOST_PORT=$($allocatedPorts["NANIHOLD_WEB_HOST_PORT"])"
    ) -join "`n"
    Write-Utf8NoBom -Path $ComposeEnv -Content "$composeEnvironment`n"

    Write-Host "Initialized isolated local verification state."
    Write-Host "Claude Code: $claudeVersion"
    Write-Host "Model: claude-haiku-4-5-20251001 / low"
}

function Start-LocalReview {
    $envMap = Get-EnvMap
    $composeMap = Get-ComposeMap
    if (-not (Test-Path -LiteralPath $NaniholdConfig -PathType Leaf)) {
        throw "Generated Nanihold config is missing."
    }
    if (-not (Test-Path -LiteralPath $PilotConfig -PathType Leaf)) {
        throw "Generated PilotHost config is missing."
    }
    Start-PilotHost
    try {
        Invoke-Compose @("up", "-d", "lethe")
        Wait-Http `
            -Uri "http://127.0.0.1:$($composeMap["LETHE_HTTP_HOST_PORT"])/api/operational-events/stats" `
            -BearerToken $envMap["LETHE_NANIHOLD_TOKEN"] `
            -Name "LETHE"
        Invoke-Compose @("build", "api", "web")
        Invoke-Compose @(
            "run", "--rm", "--no-deps", "api",
            "vsm", "verification", "commission",
            "--config", "/workspace/.local-verification/vsm.toml"
        )
        Invoke-Compose @("up", "-d", "--no-build", "--no-deps", "api", "web")
        Wait-Http `
            -Uri "http://127.0.0.1:$($composeMap["NANIHOLD_API_HOST_PORT"])/api/data-spaces" `
            -BearerToken $envMap["NANIHOLD_API_BEARER_TOKEN"] `
            -Name "Nanihold API"
    } catch {
        try {
            Invoke-Compose @("down")
        } finally {
            Stop-PilotHost
        }
        throw
    }
    Write-Host "Nanihold local review is ready: http://localhost:$($composeMap["NANIHOLD_WEB_HOST_PORT"])"
    Write-Host "Use .\local-review.cmd token to print the WebUI Bearer token."
    Write-Host "No model is called until you send a Conversation message or run smoke."
}

function Invoke-LiveSmoke {
    $envMap = Get-EnvMap
    $composeMap = Get-ComposeMap
    $body = @{
        text = "ローカル確認です。現在のモデル名と、書き込み操作を行わないモードであることを簡潔に説明してください。"
        idempotency_key = "local-live-smoke:$([guid]::NewGuid())"
        force_new_pilot = $true
    } | ConvertTo-Json
    $response = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:$($composeMap["NANIHOLD_API_HOST_PORT"])/api/conversations/conversation:local-verification/messages" `
        -Headers @{ Authorization = "Bearer $($envMap["NANIHOLD_API_BEARER_TOKEN"])" } `
        -ContentType "application/json; charset=utf-8" `
        -Body $body `
        -TimeoutSec 150
    $response | ConvertTo-Json -Depth 10
}

function Show-Status {
    $envMap = Get-EnvMap
    $composeMap = Get-ComposeMap
    Invoke-Compose @("ps")
    Wait-Http `
        -Uri "http://127.0.0.1:$($composeMap["PILOT_HOST_PORT"])/health" `
        -BearerToken $envMap["PILOT_HOST_BEARER_TOKEN"] `
        -Name "PilotHost"
    Wait-Http `
        -Uri "http://127.0.0.1:$($composeMap["LETHE_HTTP_HOST_PORT"])/api/operational-events/stats" `
        -BearerToken $envMap["LETHE_NANIHOLD_TOKEN"] `
        -Name "LETHE"
    Wait-Http `
        -Uri "http://127.0.0.1:$($composeMap["NANIHOLD_API_HOST_PORT"])/api/data-spaces" `
        -BearerToken $envMap["NANIHOLD_API_BEARER_TOKEN"] `
        -Name "Nanihold API"
    Write-Host "PilotHost, LETHE, and Nanihold API are ready."
}

function Show-Logs {
    Invoke-Compose @("logs", "--tail", "100", "lethe", "api", "web")
    if (Test-Path -LiteralPath $PilotLog) {
        Get-Content -LiteralPath $PilotLog -Tail 100
    }
}

function Stop-LocalReview {
    try {
        Invoke-Compose @("down")
    } finally {
        Stop-PilotHost
    }
    Write-Host "Stopped local review services. Durable local state was preserved."
}

switch ($Command) {
    "help" {
        @"
Usage:
  .\local-review.cmd init
  .\local-review.cmd up
  .\local-review.cmd token
  .\local-review.cmd smoke
  .\local-review.cmd status
  .\local-review.cmd logs
  .\local-review.cmd down

init   Generate ignored local secrets and exact Claude/Pilot configuration.
up     Start PilotHost, LETHE, commission the DataSpace, then start API/WebUI.
smoke  Make one real Haiku/low/tools-disabled Interface turn (max USD 0.05).
down   Stop processes without deleting the local Event Ledger.
"@
    }
    "init" { Initialize-LocalReview }
    "up" { Start-LocalReview }
    "status" { Show-Status }
    "smoke" { Invoke-LiveSmoke }
    "token" {
        $envMap = Get-EnvMap
        $envMap["NANIHOLD_API_BEARER_TOKEN"]
    }
    "logs" { Show-Logs }
    "down" { Stop-LocalReview }
}
