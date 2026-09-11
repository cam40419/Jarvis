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
    $env:JARVIS_ENVIRONMENT = "development"
    $env:JARVIS_PUBLIC_ORIGIN = "http://localhost:8000"
    $env:JARVIS_RP_ID = "localhost"
    $env:JARVIS_DEV_LOGIN_ENABLED = "false"
    $env:JARVIS_MODEL_PROVIDER = if ($TestRunner) { 'local' } else { 'openai' }
    & $python -c "from jarvis.config import Settings; s=Settings(); print('Assistant mode:', s.model_provider)"
    if ($LASTEXITCODE -ne 0) { throw 'Set JARVIS_OPENAI_API_KEY in .env, or use -TestRunner.' }
    if ($Memory) {
        $env:JARVIS_STORAGE_BACKEND = "memory"
    } else {
        $env:JARVIS_STORAGE_BACKEND = "postgres"
        $env:JARVIS_DATABASE_URL = $DatabaseUrl
        docker compose -f deploy/compose/compose.yaml up -d --wait
        if ($LASTEXITCODE -ne 0) { throw 'Database startup failed. Check Docker Desktop.' }
        & $python -m jarvis.migrate
        if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
        & $python -m jarvis.seed
        if ($LASTEXITCODE -ne 0) { throw 'Development identity setup failed.' }
    }
    if ($Enroll) {
        & $python -m jarvis.identity_admin enroll `
            --actor-id 11111111-1111-4111-8111-111111111111 `
            --household-id 22222222-2222-4222-8222-222222222222
        if ($LASTEXITCODE -ne 0) { throw 'Enrollment token creation failed.' }
    }
    if ($DevelopmentLogin -or $Memory) {
        $env:JARVIS_DEV_LOGIN_TOKEN = & $python -c "import secrets; print(secrets.token_urlsafe(32))"
        if ($LASTEXITCODE -ne 0) { throw 'Development token generation failed.' }
        $env:JARVIS_DEV_LOGIN_ENABLED = "true"
        Write-Host "Development login token: $env:JARVIS_DEV_LOGIN_TOKEN"
    }
    Write-Host "Open http://localhost:8000/login (use localhost, not 127.0.0.1)."
    & $python -m uvicorn jarvis.api.app:app --host 127.0.0.1 --port 8000 --reload
} finally {
    Pop-Location
}
