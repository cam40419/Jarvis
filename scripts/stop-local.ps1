# Ask Uvicorn to finish active requests and run the FastAPI shutdown handlers.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$request = Join-Path $repoRoot '.local\simon-stop.request'
$task = Get-ScheduledTask -TaskName 'Simon-Local'
if ($task.State -ne 'Running') {
    Write-Host 'Simon is already stopped.'
    return
}
Set-Content -LiteralPath $request -Value ([DateTime]::UtcNow.ToString('o'))
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 500
    $task = Get-ScheduledTask -TaskName 'Simon-Local'
    if ($task.State -ne 'Running') {
        Write-Host 'Simon stopped gracefully.'
        return
    }
}
throw 'Simon did not stop within 30 seconds. Check .local/logs/simon.log.'
