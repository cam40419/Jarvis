param([switch]$Check)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $Check -and (Test-Path -LiteralPath (Join-Path $repoRoot '.local\maintenance.request'))) {
    Write-Host 'Maintenance is active; the server was not started.'
    return
}
$stopRequest = Join-Path $repoRoot '.local\simon-stop.request'
if (-not $Check -and (Test-Path -LiteralPath $stopRequest)) {
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
        Start-Transcript -Path (Join-Path $logDirectory 'configured-server.log') -Append | Out-Null
        $transcriptStarted = $true
    }
    # Honors the operator's .env: no embedded account IDs, domain, database or model overrides.
    & $python scripts/check_startup.py server
    if ($LASTEXITCODE -ne 0) { throw 'Configure production HTTPS settings before starting.' }
    if ($Check) { return }
    & $python -m simon.migrate
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed. Start PostgreSQL first.' }
    & $python scripts/run_local.py
    if ($LASTEXITCODE -ne 0) { throw 'Simon exited unexpectedly.' }
} finally {
    Pop-Location
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
