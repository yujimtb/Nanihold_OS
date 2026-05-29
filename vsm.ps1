$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv-win\Scripts\python.exe"
if (Test-Path $Python) {
    & $Python -m vsm @args
} else {
    & python -m vsm @args
}
exit $LASTEXITCODE
