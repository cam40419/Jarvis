# Register Simon's user-session startup, recovery, and backup tasks.
param([switch]$OnlyWorkflow)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$pythonw = Join-Path $repoRoot 'venv\Scripts\pythonw.exe'
$hiddenLauncher = Join-Path $PSScriptRoot 'run_hidden.py'
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw 'Install the project dependencies in venv before registering local tasks.'
}
$startup = New-ScheduledTaskTrigger -AtLogOn -User $user
$backup = New-ScheduledTaskTrigger -Daily -At '03:00'
$recovery = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

function Register-SimonTask {
    param(
        [string]$Name,
        [string]$Script,
        [string]$ExtraArguments,
        $Trigger,
        [timespan]$Limit,
        [string]$Description
    )
    $scriptPath = Join-Path $PSScriptRoot $Script
    $arguments = "`"$hiddenLauncher`" `"$scriptPath`""
    if ($ExtraArguments) { $arguments += " $ExtraArguments" }
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments `
        -WorkingDirectory $repoRoot
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit $Limit `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
        -Settings $settings -Principal $principal -Description $Description -Force | Out-Null
    Write-Host "Registered $Name for $user"
}

if (-not $OnlyWorkflow) {
    Register-SimonTask -Name 'Simon-PostgreSQL' -Script 'start-local.ps1' `
        -ExtraArguments '-DatabaseOnly' -Trigger $startup -Limit (New-TimeSpan -Minutes 10) `
        -Description 'Start Simon PostgreSQL in Docker Desktop at user logon.'
    Register-SimonTask -Name 'Simon-Local' -Script 'start-local.ps1' `
        -ExtraArguments '' -Trigger $startup -Limit ([timespan]::Zero) `
        -Description 'Run the local Simon API without a reload watcher.'
}
Register-SimonTask -Name 'Simon-Workflow' -Script 'start-workflow-worker.ps1' `
    -ExtraArguments '' -Trigger $startup -Limit ([timespan]::Zero) `
    -Description 'Run read-only Simon workflows for project and home inventory checks.'
if (-not $OnlyWorkflow) {
    Register-SimonTask -Name 'Simon-Backup' -Script 'backup-local.ps1' `
        -ExtraArguments '' -Trigger $backup -Limit (New-TimeSpan -Hours 2) `
        -Description 'Back up the Simon PostgreSQL database daily at 3 AM.'
    Register-SimonTask -Name 'Simon-Recovery' -Script 'recover-local.ps1' `
        -ExtraArguments '' -Trigger $recovery -Limit (New-TimeSpan -Minutes 2) `
        -Description 'Restart the PostgreSQL, Simon API, and workflow tasks if a service is down.'
}
