<#
Run Nanihold OS development commands from the Codex app's Windows shell.

The actual development environment is WSL + Docker Compose. This wrapper keeps
Codex out of the Windows Python environment and always executes project commands
inside the WSL checkout.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("help", "doctor", "up", "ps", "install", "test", "vsm", "exec", "compose", "wsl")]
    [string] $Command = "help",

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]] $CommandArgs = @()
)

$ErrorActionPreference = "Stop"

$ProjectDir = if ($env:NANIHOLD_WSL_PROJECT_DIR) {
    $env:NANIHOLD_WSL_PROJECT_DIR
} else {
    "/home/user/projects/Nanihold_OS"
}

$DistroArgs = @()
if ($env:NANIHOLD_WSL_DISTRO) {
    $DistroArgs = @("-d", $env:NANIHOLD_WSL_DISTRO)
}

function Invoke-InWsl {
    param([Parameter(Mandatory = $true)][string[]] $Argv)

    & wsl @DistroArgs --cd $ProjectDir -- @Argv
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

function Show-Help {
    @"
Usage:
  .\codex-dev.cmd <command> [args...]
  powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\codex-dev.ps1 <command> [args...]

Commands:
  doctor             Check WSL, Docker, and Compose visibility.
  up                 Start the Docker Compose app service.
  ps                 Show Docker Compose service status.
  install            Run: docker compose exec -T app python -m pip install -e .
  test [args...]     Run pytest in the app service.
  vsm [args...]      Run the vsm CLI in the app service.
  exec [args...]     Run an arbitrary command in the app service.
  compose [args...]  Run docker compose with the supplied args in WSL.
  wsl [args...]      Run an arbitrary command in the WSL project directory.

Environment overrides:
  NANIHOLD_WSL_PROJECT_DIR  Default: /home/user/projects/Nanihold_OS
  NANIHOLD_WSL_DISTRO       Optional WSL distro name, for example Ubuntu
"@
}

switch ($Command) {
    "help" {
        Show-Help
    }
    "doctor" {
        Invoke-InWsl @("bash", "-lc", "set -e; pwd; whoami; id; docker version --format '{{.Server.Version}}'; docker compose version; docker compose ps")
    }
    "up" {
        Invoke-InWsl @("docker", "compose", "up", "-d", "app")
    }
    "ps" {
        Invoke-InWsl @("docker", "compose", "ps")
    }
    "install" {
        Invoke-InWsl @("docker", "compose", "exec", "-T", "app", "python", "-m", "pip", "install", "-e", ".")
    }
    "test" {
        Invoke-InWsl (@("docker", "compose", "exec", "-T", "app", "python", "-m", "pytest") + $CommandArgs)
    }
    "vsm" {
        Invoke-InWsl (@("docker", "compose", "exec", "-T", "app", "vsm") + $CommandArgs)
    }
    "exec" {
        if ($CommandArgs.Count -eq 0) {
            throw "exec requires a command to run in the app service."
        }
        Invoke-InWsl (@("docker", "compose", "exec", "-T", "app") + $CommandArgs)
    }
    "compose" {
        if ($CommandArgs.Count -eq 0) {
            throw "compose requires docker compose arguments."
        }
        Invoke-InWsl (@("docker", "compose") + $CommandArgs)
    }
    "wsl" {
        if ($CommandArgs.Count -eq 0) {
            throw "wsl requires a command to run in the WSL project directory."
        }
        Invoke-InWsl $CommandArgs
    }
}
