# Compatibility launcher for existing Simon-Workflow scheduled tasks.
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start-assistant-worker.ps1') @args
exit $LASTEXITCODE
