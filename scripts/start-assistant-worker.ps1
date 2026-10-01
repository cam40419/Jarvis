param(
    [switch]$Once,
    [ValidateNotNullOrEmpty()][string]$DatabaseUrl,
    [guid]$HouseholdId,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project environment first.' }
if ($Once -and $Check) { throw 'Choose either -Once or -Check.' }
if (-not $Check -and (Test-Path -LiteralPath (Join-Path $repoRoot '.local\maintenance.request'))) {
    Write-Host 'Maintenance is active; assistant worker remains stopped.'
    exit 0
}
$logDirectory = Join-Path $repoRoot '.local\logs'
$logFile = Join-Path $logDirectory 'assistant-worker.log'
$stopFile = Join-Path $repoRoot '.local\assistant-worker-stop.request'
$previousDatabaseUrl = [Environment]::GetEnvironmentVariable('SIMON_DATABASE_URL', 'Process')
$previousHouseholdId = [Environment]::GetEnvironmentVariable('SIMON_ACCOUNT_HOUSEHOLD_ID', 'Process')
Push-Location $repoRoot
try {
    # Settings loads .env. Only explicitly supplied compatibility parameters override it.
    if ($PSBoundParameters.ContainsKey('DatabaseUrl')) { $env:SIMON_DATABASE_URL = $DatabaseUrl }
    if ($PSBoundParameters.ContainsKey('HouseholdId')) {
        $env:SIMON_ACCOUNT_HOUSEHOLD_ID = $HouseholdId.ToString()
    }
    if (-not $Check) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        if (-not $Once) {
            Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
        }
        # Do not transcript command-line arguments: DatabaseUrl can contain credentials.
        Add-Content -LiteralPath $logFile -Value "$([DateTime]::UtcNow.ToString('o')) Assistant launcher starting"
    }
    & $python -m simon.assistant_worker --check
    if ($LASTEXITCODE -ne 0) { throw 'Assistant worker configuration is not ready.' }
    if ($Check) { return }
    & $python -m simon.migrate
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed. Start PostgreSQL first.' }
    $workerArgs = @('-m', 'simon.assistant_worker', '--log-file', $logFile)
    if ($Once) { $workerArgs += '--once' }
    else { $workerArgs += @('--stop-file', $stopFile) }
    & $python @workerArgs
    if ($LASTEXITCODE -ne 0) { throw 'Assistant worker exited with an error.' }
} catch {
    if (-not $Check -and (Test-Path -LiteralPath $logDirectory)) {
        Add-Content -LiteralPath $logFile -Value "$([DateTime]::UtcNow.ToString('o')) Assistant launcher failed; check configuration and service availability"
    }
    throw
} finally {
    [Environment]::SetEnvironmentVariable('SIMON_DATABASE_URL', $previousDatabaseUrl, 'Process')
    [Environment]::SetEnvironmentVariable('SIMON_ACCOUNT_HOUSEHOLD_ID', $previousHouseholdId, 'Process')
    Pop-Location
}
