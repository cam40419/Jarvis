param([switch]$Check, [switch]$Once, [switch]$Supervised)
$ErrorActionPreference = 'Stop'
if ($Check -and $Once) { throw 'Choose either -Once or -Check.' }
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Check -and (Test-Path -LiteralPath (Join-Path $repoRoot '.local\maintenance.request'))) {
    Write-Host 'Maintenance is active; the agent dispatcher was not started.'
    return
}
$stopRequest = Join-Path $repoRoot '.local\agent-dispatcher-stop.request'
if ($Supervised -and -not $Check -and (Test-Path -LiteralPath $stopRequest)) {
    Write-Host 'Agent stop request is active; use resume-local.ps1 to resume.'
    $global:LASTEXITCODE = 0
    return
}
if (-not $Check -and -not $Once -and -not $Supervised -and
    (Test-Path -LiteralPath $stopRequest)) {
    # Clear only at explicit launch, before setup, so a later stop request survives.
    Remove-Item -LiteralPath $stopRequest
}
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project environment first.' }
$logDirectory = Join-Path $repoRoot '.local\logs'
$logFile = Join-Path $logDirectory 'agent-dispatcher.log'
Push-Location $repoRoot
try {
    if (-not $Check) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        Add-Content -LiteralPath $logFile -Value "$([DateTime]::UtcNow.ToString('o')) Agent launcher starting"
    }
    & $python scripts/check_startup.py agents
    if ($LASTEXITCODE -ne 0) { throw 'Agent dispatcher configuration is not ready.' }
    if ($Check) { return }
    $prepareArgs = @('-m', 'simon.worker_startup', '--log-file', $logFile,
        '--maintenance-file', (Join-Path $repoRoot '.local\maintenance.request'))
    if (-not $Once) { $prepareArgs += @('--stop-file', $stopRequest) }
    & $python @prepareArgs
    if ($LASTEXITCODE -eq 3) { $global:LASTEXITCODE = 0; return }
    if ($LASTEXITCODE -ne 0) { throw 'Agent database preparation failed; inspect its log.' }
    $dispatcherArgs = @('-m', 'simon.agent_dispatcher', '--log-file', $logFile)
    if ($Once) { $dispatcherArgs += '--once' }
    else { $dispatcherArgs += @('--stop-file', $stopRequest) }
    & $python @dispatcherArgs
    if ($LASTEXITCODE -ne 0) { throw 'Agent dispatcher exited with an error.' }
} catch {
    if (-not $Check -and (Test-Path -LiteralPath $logDirectory)) {
        Add-Content -LiteralPath $logFile -Value "$([DateTime]::UtcNow.ToString('o')) Agent launcher failed; check configuration and service availability"
    }
    throw
} finally {
    Pop-Location
}
