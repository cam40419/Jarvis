param(
    [switch]$Once,
    [string]$DatabaseUrl = "postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis",
    [guid]$HouseholdId = 'eff4172f-8892-5123-821a-55fed2969246'
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project environment first.' }
Push-Location $repoRoot
try {
    $env:SIMON_STORAGE_BACKEND = 'postgres'
    $env:SIMON_DATABASE_URL = $DatabaseUrl
    $env:SIMON_MODEL_PROVIDER = 'openai'
    $env:SIMON_HOME_HOUSEHOLD_ID = $HouseholdId.ToString()
    & $python -m simon.migrate
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed. Start PostgreSQL first.' }
    $workerArgs = @('-m', 'simon.workflow_worker')
    if ($Once) { $workerArgs += '--once' }
    & $python @workerArgs
    if ($LASTEXITCODE -ne 0) { throw 'Workflow worker exited with an error.' }
} finally {
    Pop-Location
}
