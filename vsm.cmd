@echo off
set "VSM_ROOT=%~dp0"
if exist "%VSM_ROOT%.venv-win\Scripts\python.exe" (
    "%VSM_ROOT%.venv-win\Scripts\python.exe" -m vsm %*
) else (
    python -m vsm %*
)
