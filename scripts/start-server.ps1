param(
    [string]$PublicOrigin = 'https://camrobbins.com',
    [string]$PublicPath = '/simon',
    [string]$DatabaseUrl = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis',
    [guid]$HouseholdId = '22222222-2222-4222-8222-222222222222',
    [guid]$ActorId = '11111111-1111-4111-8111-111111111111',
    [switch]$Enroll,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install .[dev,postgres] in venv first.' }
Push-Location $repoRoot
try {
    $env:SIMON_ENVIRONMENT = 'production'
    $env:SIMON_PUBLIC_ORIGIN = $PublicOrigin
    $env:SIMON_PUBLIC_PATH = $PublicPath
    $env:SIMON_RP_ID = ([uri]$PublicOrigin).DnsSafeHost
    $env:SIMON_STORAGE_BACKEND = 'postgres'
    $env:SIMON_DATABASE_URL = $DatabaseUrl
    $env:SIMON_DEV_LOGIN_ENABLED = 'false'
    $env:SIMON_MODEL_PROVIDER = 'openai'
    $env:SIMON_HOME_HOUSEHOLD_ID = $HouseholdId.ToString()
    & $python -c "from simon.config import Settings; s=Settings(); print('Configuration valid:', s.public_origin + s.public_path)"
    if ($LASTEXITCODE -ne 0) { throw 'Server configuration is invalid.' }
    if ($Check) { return }
    docker compose -f deploy/compose/compose.yaml up -d --wait
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and retry.' }
    & $python -m simon.migrate
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
    if ($Enroll) {
        & $python -m simon.identity_admin enroll --actor-id $ActorId --household-id $HouseholdId
        if ($LASTEXITCODE -ne 0) { throw 'Enrollment failed. Check the existing household and actor IDs.' }
        return
    }
    Write-Host "Simon origin ready for the tunnel: $PublicOrigin$PublicPath/login"
    # One process owns live voice sideband connections and background home monitoring.
    & $python -m uvicorn simon.api.app:app --host 127.0.0.1 --port 8000 `
        --proxy-headers --forwarded-allow-ips 127.0.0.1 --no-access-log
    if ($LASTEXITCODE -ne 0) { throw 'Simon exited unexpectedly.' }
} finally { Pop-Location }
