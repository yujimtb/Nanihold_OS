@echo off
setlocal
pwsh -NoProfile -File "%~dp0scripts\local-review.ps1" %*
