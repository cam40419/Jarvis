param([ValidateRange(1, 3600)][int]$TimeoutSeconds = 60)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$request = Join-Path $repoRoot '.local\assistant-worker-stop.request'
New-Item -ItemType Directory -Path (Split-Path -Parent $request) -Force | Out-Null
Set-Content -LiteralPath $request -Value ([DateTime]::UtcNow.ToString('o'))
$task = Get-ScheduledTask -TaskName 'Simon-Assistant' -ErrorAction SilentlyContinue
if (-not $task -or $task.State -ne 'Running') {
    Write-Host 'Assistant stop recorded. Recovery will leave it stopped until explicit startup.'
    return
}
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 500
    $task = Get-ScheduledTask -TaskName 'Simon-Assistant' -ErrorAction SilentlyContinue
    if (-not $task -or $task.State -ne 'Running') {
        Write-Host 'Assistant worker stopped gracefully.'
        return
    }
}
throw 'Assistant is still draining work. Stop request remains active; inspect .local/logs/assistant-worker.log.'
