param([switch]$Start)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repoRoot 'venv\Scripts\pythonw.exe'
$launcher = Join-Path $PSScriptRoot 'run_hidden.py'
$dispatcher = Join-Path $PSScriptRoot 'start-agent-dispatcher.ps1'
if (-not (Test-Path -LiteralPath $pythonw)) { throw 'Install the project environment first.' }
foreach ($requiredScript in @($launcher, $dispatcher, (Join-Path $PSScriptRoot 'check_startup.py'))) {
    if (-not (Test-Path -LiteralPath $requiredScript -PathType Leaf)) {
        throw 'The dispatcher launch scripts are incomplete. Restore the checkout first.'
    }
}
& $dispatcher -Check
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$launcher`" `"$dispatcher`"" -WorkingDirectory $repoRoot
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([timespan]::Zero) -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'Simon-Agents' -Action $action `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) -Settings $settings `
    -Principal $principal -Description 'Execute explicitly queued Simon agent team runs.' `
    -Force | Out-Null
if ($Start) { Start-ScheduledTask -TaskName 'Simon-Agents' }
Write-Host 'Registered Simon-Agents. This user must sign in before the task can run.'
