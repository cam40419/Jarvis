param(
    [switch]$Once,
    [ValidateNotNullOrEmpty()][string]$DatabaseUrl,
    [guid]$WorkspaceId,
    [switch]$Check,
    [switch]$Supervised
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot "venv\Scripts\python.exe"
if ($Once -and $Check) { throw 'Choose either -Once or -Check.' }
if (-not $Check -and (Test-Path -LiteralPath (Join-Path $repoRoot '.local\maintenance.request'))) {
    Write-Host 'Maintenance is active; assistant worker remains stopped.'
    exit 0
}
$logDirectory = Join-Path $repoRoot '.local\logs'
$logFile = Join-Path $logDirectory 'assistant-worker.log'
$stopFile = Join-Path $repoRoot '.local\assistant-worker-stop.request'
if ($Supervised -and -not $Check -and (Test-Path -LiteralPath $stopFile)) {
    Write-Host 'Assistant stop request is active; use resume-local.ps1 to resume.'
    $global:LASTEXITCODE = 0
    return
}
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project environment first.' }
$previousDatabaseUrl = [Environment]::GetEnvironmentVariable('SIMON_DATABASE_URL', 'Process')
$previousWorkspaceId = [Environment]::GetEnvironmentVariable('SIMON_ACCOUNT_WORKSPACE_ID', 'Process')
Push-Location $repoRoot
try {
    # Settings loads .env. Only explicitly supplied parameters override it.
    if ($PSBoundParameters.ContainsKey('DatabaseUrl')) { $env:SIMON_DATABASE_URL = $DatabaseUrl }
    if ($PSBoundParameters.ContainsKey('WorkspaceId')) {
        $env:SIMON_ACCOUNT_WORKSPACE_ID = $WorkspaceId.ToString()
    }
    if (-not $Check) {
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        if (-not $Once -and -not $Supervised) {
            Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
        }
        # Do not transcript command-line arguments: DatabaseUrl can contain credentials.
        Add-Content -LiteralPath $logFile -Value "$([DateTime]::UtcNow.ToString('o')) Assistant launcher starting"
    }
    & $python -m simon.assistant_worker --check
    if ($LASTEXITCODE -ne 0) { throw 'Assistant worker configuration is not ready.' }
    if ($Check) { return }
    $prepareArgs = @('-m', 'simon.worker_startup', '--log-file', $logFile,
        '--maintenance-file', (Join-Path $repoRoot '.local\maintenance.request'))
    if (-not $Once) { $prepareArgs += @('--stop-file', $stopFile) }
    & $python @prepareArgs
    if ($LASTEXITCODE -eq 3) { $global:LASTEXITCODE = 0; return }
    if ($LASTEXITCODE -ne 0) { throw 'Assistant database preparation failed; inspect its log.' }
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
    [Environment]::SetEnvironmentVariable('SIMON_ACCOUNT_WORKSPACE_ID', $previousWorkspaceId, 'Process')
    Pop-Location
}
