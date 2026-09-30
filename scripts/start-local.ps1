param(
    [switch]$Check,
    [switch]$DatabaseOnly,
    [string]$DatabaseUrl = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis',
    [guid]$HouseholdId = 'eff4172f-8892-5123-821a-55fed2969246',
    [guid]$ActorId = '31de7ca5-7ea4-5614-b2b6-099dacc91b0e'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
$compose = Join-Path $repoRoot 'deploy\compose\compose.yaml'
$logDirectory = Join-Path $repoRoot '.local\logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logName = if ($DatabaseOnly) { 'postgres.log' } else { 'simon.log' }
$transcriptStarted = $false

function Wait-DockerEngine {
    $desktop = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & docker info --format '{{.ServerVersion}}' *> $null
            if ($LASTEXITCODE -eq 0) { return }
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($attempt -eq 0 -and (Test-Path -LiteralPath $desktop) -and
            -not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath $desktop -WindowStyle Hidden
        }
        Start-Sleep -Seconds 5
    }
    throw 'Docker Desktop did not become ready within five minutes.'
}

try {
    Start-Transcript -Path (Join-Path $logDirectory $logName) -Append | Out-Null
    $transcriptStarted = $true
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'Install the project dependencies in venv first.'
    }
    $env:SIMON_ENVIRONMENT = 'development'
    $env:SIMON_PUBLIC_ORIGIN = 'http://localhost:8000'
    $env:SIMON_PUBLIC_PATH = ''
    $env:SIMON_RP_ID = 'localhost'
    $env:SIMON_STORAGE_BACKEND = 'postgres'
    $env:SIMON_DATABASE_URL = $DatabaseUrl
    $env:SIMON_DEV_LOGIN_ENABLED = 'false'
    Remove-Item Env:SIMON_DEV_LOGIN_TOKEN -ErrorAction SilentlyContinue
    $env:SIMON_MODEL_PROVIDER = if ($DatabaseOnly) { 'local' } else { 'openai' }
    $env:SIMON_HOME_HOUSEHOLD_ID = $HouseholdId.ToString()
    $env:SIMON_ACCOUNT_ADMIN_ACTOR_ID = $ActorId.ToString()

    Push-Location $repoRoot
    try {
        & $python -c "from simon.config import Settings; s=Settings(); assert not s.dev_login_enabled and s.storage_backend == 'postgres'; print('Local configuration valid:', s.public_origin)"
        if ($LASTEXITCODE -ne 0) { throw 'Local configuration is invalid.' }
        Wait-DockerEngine
        docker compose -f $compose up -d --wait
        if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL startup failed.' }
        if ($DatabaseOnly) {
            Write-Host 'PostgreSQL is healthy.'
            return
        }
        & $python -m simon.migrate
        if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
        & $python scripts/check_local_account.py
        if ($LASTEXITCODE -ne 0) { throw 'The cam40419 administrator account is not ready.' }
        if ($Check) { return }
        Write-Host 'Simon is starting at http://localhost:8000/login'
        & $python scripts/run_local.py
        if ($LASTEXITCODE -ne 0) { throw 'Simon exited unexpectedly.' }
    } finally {
        Pop-Location
    }
} finally {
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
