# Restart monitoring after a runner exits or this user signs in again.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$scriptPath = Join-Path $PSScriptRoot 'start-printer-recovery.ps1'
$pythonw = Join-Path $repoRoot 'venv\Scripts\pythonw.exe'
$hiddenLauncher = Join-Path $PSScriptRoot 'run_hidden.py'
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw 'Install the project dependencies in venv before registering printer recovery.'
}
$arguments = '"' + $hiddenLauncher + '" "' + $scriptPath + '"'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $repoRoot
$periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([timespan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'Simon-Printer-Recovery' -Action $action -Trigger @($periodic, $logon) `
    -Settings $settings -Principal $principal -Description 'Resume monitoring and automatic handoff for already-started A1 batches; never replay uncertain print starts.' -Force | Out-Null
Write-Host 'Registered Simon-Printer-Recovery (every minute; no run duration limit).'
