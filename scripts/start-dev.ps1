param(
    [switch]$Enroll,
    [switch]$DevelopmentLogin,
    [switch]$Memory,
    [switch]$TestRunner,
    [string]$DatabaseUrl = "postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create venv and install .[dev,postgres] using the README first.'
}
if ($Memory -and $Enroll) { throw 'Passkey enrollment through this launcher requires PostgreSQL.' }

Push-Location $repoRoot
try {
    $env:SIMON_ENVIRONMENT = "development"
    $env:SIMON_PUBLIC_ORIGIN = "http://localhost:8000"
    $env:SIMON_PUBLIC_PATH = ""
    $env:SIMON_RP_ID = "localhost"
    $env:SIMON_DEV_LOGIN_ENABLED = "false"
    $env:SIMON_MODEL_PROVIDER = if ($TestRunner) { 'local' } else { 'openai' }
    & $python -c "from simon.config import Settings; s=Settings(); print('Assistant mode:', s.model_provider)"
    if ($LASTEXITCODE -ne 0) { throw 'Set SIMON_OPENAI_API_KEY in .env, or use -TestRunner.' }
    if ($Memory) {
        $env:SIMON_STORAGE_BACKEND = "memory"
    } else {
        $env:SIMON_STORAGE_BACKEND = "postgres"
        $env:SIMON_DATABASE_URL = $DatabaseUrl
        docker compose -f deploy/compose/compose.yaml up -d --wait
        if ($LASTEXITCODE -ne 0) { throw 'Database startup failed. Check Docker Desktop.' }
        & $python -m simon.migrate
        if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
        & $python -m simon.seed
        if ($LASTEXITCODE -ne 0) { throw 'Development identity setup failed.' }
    }
    if ($Enroll) {
        & $python -m simon.identity_admin enroll `
            --actor-id 11111111-1111-4111-8111-111111111111 `
            --household-id 22222222-2222-4222-8222-222222222222
        if ($LASTEXITCODE -ne 0) { throw 'Enrollment token creation failed.' }
    }
    if ($DevelopmentLogin -or $Memory) {
        $env:SIMON_DEV_LOGIN_TOKEN = & $python -c "import secrets; print(secrets.token_urlsafe(32))"
        if ($LASTEXITCODE -ne 0) { throw 'Development token generation failed.' }
        $env:SIMON_DEV_LOGIN_ENABLED = "true"
        Write-Host "Development login token: $env:SIMON_DEV_LOGIN_TOKEN"
    }
    Write-Host "Open http://localhost:8000/login (use localhost, not 127.0.0.1)."
    & $python -m uvicorn simon.api.app:app --host 127.0.0.1 --port 8000 --reload --reload-include '.env'
} finally {
    Pop-Location
}
