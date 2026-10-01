# Recover registered services unless the operator requested a stop or maintenance.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (Test-Path -LiteralPath (Join-Path $repoRoot '.local\maintenance.request')) { return }
$compose = Join-Path $repoRoot 'deploy\compose\compose.yaml'
$postgresTask = Get-ScheduledTask -TaskName 'Simon-PostgreSQL' -ErrorAction SilentlyContinue
$simonTask = Get-ScheduledTask -TaskName 'Simon-Local' -ErrorAction SilentlyContinue
$workflowTask = Get-ScheduledTask -TaskName 'Simon-Workflow' -ErrorAction SilentlyContinue
$agentsTask = Get-ScheduledTask -TaskName 'Simon-Agents' -ErrorAction SilentlyContinue
$tunnelTask = Get-ScheduledTask -TaskName 'Simon-Tunnel' -ErrorAction SilentlyContinue
$httpsPlan = $null
$planPath = Join-Path $repoRoot '.local\https-plan.json'

function Test-RecoveryAllowed {
    param($Task, [string]$StopRequest)
    return $Task -and $Task.State -eq 'Ready' -and
        -not (Test-Path -LiteralPath (Join-Path $repoRoot '.local\maintenance.request')) -and
        -not ($StopRequest -and (Test-Path -LiteralPath (Join-Path $repoRoot $StopRequest)))
}

$databaseHealthy = $false
try {
    $container = (& docker compose -f $compose ps -q postgres 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $container) {
        $status = & docker inspect --format '{{.State.Health.Status}}' $container 2>$null
        $databaseHealthy = $LASTEXITCODE -eq 0 -and $status -eq 'healthy'
    }
} catch { $databaseHealthy = $false }
if (-not $databaseHealthy -and (Test-RecoveryAllowed $postgresTask '')) {
    Start-ScheduledTask -TaskName 'Simon-PostgreSQL'
}

$applicationHealthy = $false
try {
    $healthUri = 'http://localhost:8000/health/live'
    $healthHeaders = @{}
    if (Test-Path -LiteralPath $planPath) {
        $httpsPlan = Get-Content -LiteralPath $planPath -Raw | ConvertFrom-Json
        $healthHost = [string]$httpsPlan.health_host
        $publicPath = [string]$httpsPlan.environment_updates.SIMON_PUBLIC_PATH
        if ($healthHost -notmatch '^[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]$' -or
            $publicPath -notmatch '^(?:/[a-zA-Z0-9_-]+)*$') { throw 'Invalid HTTPS health target.' }
        # The connection stays on loopback; only the canonical Host and path vary.
        $healthUri = 'http://127.0.0.1:8000' + $publicPath + '/health/live'
        $healthHeaders = @{ Host = $healthHost }
    }
    $response = Invoke-WebRequest -Uri $healthUri -Headers $healthHeaders `
        -UseBasicParsing -TimeoutSec 5
    $applicationHealthy = $response.StatusCode -eq 200
} catch { $applicationHealthy = $false }
if (-not $applicationHealthy -and (Test-RecoveryAllowed $simonTask '.local\simon-stop.request')) {
    Start-ScheduledTask -TaskName 'Simon-Local'
}
if ($databaseHealthy -and (Test-RecoveryAllowed $workflowTask '.local\assistant-worker-stop.request')) {
    Start-ScheduledTask -TaskName 'Simon-Workflow'
}
if ($databaseHealthy -and (Test-RecoveryAllowed $agentsTask '.local\agent-dispatcher-stop.request')) {
    Start-ScheduledTask -TaskName 'Simon-Agents'
}
if ($applicationHealthy -and $httpsPlan -and $httpsPlan.provider -eq 'cloudflare' -and
    (Test-RecoveryAllowed $tunnelTask '.local\tunnel-stop.request')) {
    Start-ScheduledTask -TaskName 'Simon-Tunnel'
}
