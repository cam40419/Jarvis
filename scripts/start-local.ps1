param(
    [switch]$Check,
    [switch]$DatabaseOnly,
    [switch]$Supervised,
    [ValidateNotNullOrEmpty()][string]$DatabaseUrl,
    [guid]$WorkspaceId,
    [guid]$ActorId
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$maintenance = Join-Path $repoRoot '.local\maintenance.request'
if ($Check -and $DatabaseOnly) { throw 'Choose either -Check or -DatabaseOnly.' }
if (-not $Check -and (Test-Path -LiteralPath $maintenance)) {
    Write-Host 'Maintenance is active; local services remain stopped.'
    exit 0
}
if (-not $Check -and -not $DatabaseOnly) {
    if ($Supervised -and (Test-Path -LiteralPath (Join-Path $repoRoot '.local\simon-stop.request'))) {
        Write-Host 'Server stop request is active; use resume-local.ps1 to resume.'
        exit 0
    }
    if (-not $Supervised) {
        Remove-Item -LiteralPath (Join-Path $repoRoot '.local\simon-stop.request') -ErrorAction SilentlyContinue
    }
}
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
$compose = Join-Path $repoRoot 'deploy\compose\compose.yaml'
$logDirectory = Join-Path $repoRoot '.local\logs'
$logName = if ($DatabaseOnly) { 'postgres.log' } else { 'simon.log' }
$logFile = Join-Path $logDirectory $logName
$overrides = @{}
if ($PSBoundParameters.ContainsKey('DatabaseUrl')) { $overrides.SIMON_DATABASE_URL = $DatabaseUrl }
if ($PSBoundParameters.ContainsKey('WorkspaceId')) { $overrides.SIMON_ACCOUNT_WORKSPACE_ID = $WorkspaceId.ToString() }
if ($PSBoundParameters.ContainsKey('ActorId')) { $overrides.SIMON_ACCOUNT_ADMIN_ACTOR_ID = $ActorId.ToString() }
$previous = @{}

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
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'Install the project dependencies in venv first.'
    }
    # Preserve configured database, account, model and public origin. Overrides are explicit
    # and process-local; never transcript arguments because the database URL may hold secrets.
    foreach ($name in $overrides.Keys) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $overrides[$name], 'Process')
    }
    if (-not $Check) { New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null }

    Push-Location $repoRoot
    try {
        if ($DatabaseOnly) {
            Wait-DockerEngine
            docker compose -f $compose up -d --wait
            if ($LASTEXITCODE -ne 0) { throw 'Compose PostgreSQL startup failed.' }
            Write-Host 'Compose PostgreSQL is healthy; application settings were not changed.'
            return
        }
        & $python scripts/check_local_account.py --config-only
        if ($LASTEXITCODE -ne 0) { throw 'Persistent server configuration is not ready.' }
        if ($Check) { return }
        & $python -m simon.worker_startup --log-file $logFile --stop-file (Join-Path $repoRoot '.local\simon-stop.request') --maintenance-file $maintenance
        if ($LASTEXITCODE -eq 3) { $global:LASTEXITCODE = 0; return }
        if ($LASTEXITCODE -ne 0) { throw 'Database preparation failed; inspect the server log.' }
        & $python scripts/check_local_account.py
        if ($LASTEXITCODE -ne 0) { throw 'The configured administrator account is not ready.' }
        Write-Host 'Simon is starting with the configured public origin and model provider.'
        & $python scripts/run_local.py
        if ($LASTEXITCODE -ne 0) { throw 'Simon exited unexpectedly.' }
    } finally {
        Pop-Location
    }
} finally {
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process')
    }
}
