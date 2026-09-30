$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
$logDirectory = Join-Path $repoRoot '.local\logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$transcriptStarted = $false
try {
    Start-Transcript -Path (Join-Path $logDirectory 'backup.log') -Append | Out-Null
    $transcriptStarted = $true
    if (-not (Test-Path -LiteralPath $python)) { throw 'Python environment is missing.' }
    Push-Location $repoRoot
    try {
        & $python scripts/backup_local.py
        if ($LASTEXITCODE -ne 0) { throw 'Automatic PostgreSQL backup failed.' }
    } finally { Pop-Location }
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
