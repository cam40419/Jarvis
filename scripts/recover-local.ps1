# Keep the registered local tasks running after a clean exit or container stop.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $repoRoot 'deploy\compose\compose.yaml'
$postgresTask = Get-ScheduledTask -TaskName 'Simon-PostgreSQL'
$simonTask = Get-ScheduledTask -TaskName 'Simon-Local'
$workflowTask = Get-ScheduledTask -TaskName 'Simon-Workflow' -ErrorAction SilentlyContinue

$databaseHealthy = $false
try {
    $container = (& docker compose -f $compose ps -q postgres 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $container) {
        $status = & docker inspect --format '{{.State.Health.Status}}' $container 2>$null
        $databaseHealthy = $LASTEXITCODE -eq 0 -and $status -eq 'healthy'
    }
} catch { $databaseHealthy = $false }
if (-not $databaseHealthy -and $postgresTask.State -ne 'Running') {
    Start-ScheduledTask -TaskName 'Simon-PostgreSQL'
}

$applicationHealthy = $false
try {
    $response = Invoke-WebRequest -Uri 'http://localhost:8000/health/live' `
        -UseBasicParsing -TimeoutSec 5
    $applicationHealthy = $response.StatusCode -eq 200
} catch { $applicationHealthy = $false }
if (-not $applicationHealthy -and $simonTask.State -ne 'Running') {
    Start-ScheduledTask -TaskName 'Simon-Local'
}
if ($databaseHealthy -and $workflowTask -and $workflowTask.State -ne 'Running') {
    Start-ScheduledTask -TaskName 'Simon-Workflow'
}
