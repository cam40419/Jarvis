# Register HTTPS startup only after configuration is applied with writers stopped.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.local\maintenance.request'))) {
    throw 'Apply HTTPS during maintenance before replacing the API startup task.'
}
$apiTask = Get-ScheduledTask -TaskName 'Simon-Local' -ErrorAction SilentlyContinue
if ($apiTask -and $apiTask.State -eq 'Running') { throw 'Stop the old API before changing startup.' }
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
$pythonw = Join-Path $repoRoot 'venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Install the project environment first.' }
Push-Location $repoRoot
try {
    & $python -m simon.https_setup --check
    if ($LASTEXITCODE -ne 0) { throw 'HTTPS configuration is not ready.' }
    & (Join-Path $PSScriptRoot 'start-configured-server.ps1') -Check
    $plan = Get-Content -LiteralPath (Join-Path $repoRoot '.local\https-plan.json') -Raw | ConvertFrom-Json
    if ($plan.provider -eq 'cloudflare') { & (Join-Path $PSScriptRoot 'start-tunnel.ps1') -Check }
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([timespan]::Zero) -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    $scripts = @{ 'Simon-Local' = 'start-configured-server.ps1' }
    if ($plan.provider -eq 'cloudflare') { $scripts['Simon-Tunnel'] = 'start-tunnel.ps1' }
    foreach ($name in $scripts.Keys) {
        $launcher = Join-Path $PSScriptRoot 'run_hidden.py'
        $script = Join-Path $PSScriptRoot $scripts[$name]
        $extra = if ($name -eq 'Simon-Local') { ' -Supervised' } else { '' }
        $action = New-ScheduledTaskAction -Execute $pythonw `
            -Argument "`"$launcher`" `"$script`"$extra" -WorkingDirectory $repoRoot
        Register-ScheduledTask -TaskName $name -Action $action `
            -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) -Settings $settings `
            -Principal $principal -Description 'Run Simon using the configured HTTPS origin.' `
            -Force | Out-Null
    }
    if ($plan.provider -eq 'tailscale') {
        $oldTunnel = Get-ScheduledTask -TaskName 'Simon-Tunnel' -ErrorAction SilentlyContinue
        if ($oldTunnel) { Disable-ScheduledTask -TaskName 'Simon-Tunnel' | Out-Null }
    }
    Write-Host 'HTTPS startup registered. End maintenance, run resume-local.ps1, then start the selected proxy.'
    Write-Host 'These tasks run after this Windows user signs in.'
} finally { Pop-Location }
