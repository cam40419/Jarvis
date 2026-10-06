# Compatibility launcher for existing Simon-Workflow scheduled tasks.
param(
    [switch]$Once,
    [ValidateNotNullOrEmpty()][string]$DatabaseUrl,
    [guid]$WorkspaceId,
    [switch]$Check,
    [switch]$Supervised
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start-assistant-worker.ps1') @PSBoundParameters
exit $LASTEXITCODE
