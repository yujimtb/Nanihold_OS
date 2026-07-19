$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $Root "scripts\codex-dev.ps1") vsm @args
exit $LASTEXITCODE
