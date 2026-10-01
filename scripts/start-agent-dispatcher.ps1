param([switch]$Check, [switch]$Once)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Check -and (Test-Path -LiteralPath (Join-Path $repoRoot '.local\maintenance.request'))) {
    Write-Host 'Maintenance is active; the agent dispatcher was not started.'
    return
}
$stopRequest = Join-Path $repoRoot '.local\agent-dispatcher-stop.request'
if (-not $Check -and -not $Once -and (Test-Path -LiteralPath $stopRequest)) {
    # Clear only at explicit launch, before setup, so a later stop request survives.
    Remove-Item -LiteralPath $stopRequest
}
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project environment first.' }
$logDirectory = Join-Path $repoRoot '.local\logs'
$transcriptStarted = $false
Push-Location $repoRoot
try {
    if (-not $Check) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        Start-Transcript -Path (Join-Path $logDirectory 'agent-dispatcher.log') -Append | Out-Null
        $transcriptStarted = $true
    }
    & $python scripts/check_startup.py agents
    if ($LASTEXITCODE -ne 0) { throw 'Agent dispatcher configuration is not ready.' }
    if ($Check) { return }
    & $python -m simon.migrate
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed. Start PostgreSQL first.' }
    $dispatcherArgs = @('-m', 'simon.agent_dispatcher')
    if ($Once) { $dispatcherArgs += '--once' }
    else { $dispatcherArgs += @('--stop-file', $stopRequest) }
    & $python @dispatcherArgs
    if ($LASTEXITCODE -ne 0) { throw 'Agent dispatcher exited with an error.' }
} finally {
    Pop-Location
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
