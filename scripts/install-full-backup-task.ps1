# Hourly eligibility checks provide one verified full backup each idle day.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repoRoot 'venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Install the project Python environment first.' }
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$launcher = Join-Path $PSScriptRoot 'run_hidden.py'
$script = Join-Path $PSScriptRoot 'backup-full.ps1'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$launcher`" `"$script`"" -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) `
    -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName 'Simon-FullBackup' -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description 'Once per idle day, preserve database, local files, execution evidence and configuration; verify an isolated restore. Secrets and off-server encryption are managed separately.' -Force | Out-Null
Write-Host 'Registered Simon-FullBackup. Existing database-only backups remain enabled.'
