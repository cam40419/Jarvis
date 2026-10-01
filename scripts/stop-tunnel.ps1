$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$local = Join-Path $repoRoot '.local'
New-Item -ItemType Directory -Path $local -Force | Out-Null
Set-Content -LiteralPath (Join-Path $local 'tunnel-stop.request') -Value ([DateTime]::UtcNow.ToString('o'))
$task = Get-ScheduledTask -TaskName 'Simon-Tunnel' -ErrorAction SilentlyContinue
if (-not $task -or $task.State -ne 'Running') { return }
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ((Get-ScheduledTask -TaskName 'Simon-Tunnel').State -ne 'Running') {
        Write-Host 'HTTPS tunnel stopped; recovery will leave it stopped.'
        return
    }
}
throw 'Tunnel shutdown timed out. Check .local/logs/cloudflared.log.'
