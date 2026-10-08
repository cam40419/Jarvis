# Explicitly resume a stopped registered service; supervised retries preserve stops.
param([ValidateSet('all', 'api', 'assistant')][string]$Service = 'all')
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (Test-Path -LiteralPath (Join-Path $repoRoot '.local\maintenance.request')) {
    throw 'Finish maintenance before resuming services. Stop requests were preserved.'
}
$services = @(
    @{ Key = 'api'; Task = 'Simon-Local'; Marker = '.local\simon-stop.request' },
    @{ Key = 'assistant'; Task = 'Simon-Assistant'; Marker = '.local\assistant-worker-stop.request' }
)
foreach ($entry in $services) {
    if ($Service -ne 'all' -and $Service -ne $entry.Key) { continue }
    $task = Get-ScheduledTask -TaskName $entry.Task -ErrorAction SilentlyContinue
    if (-not $task -or $task.State -eq 'Disabled') {
        if ($Service -ne 'all') { throw 'Register and enable the selected task before resuming it.' }
        continue
    }
    $marker = Join-Path $repoRoot $entry.Marker
    if ($task.State -eq 'Running') {
        if (Test-Path -LiteralPath $marker) {
            throw 'Wait for the selected service to finish draining before resuming it.'
        }
        continue
    }
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker }
    Start-ScheduledTask -TaskName $entry.Task
    Write-Host "Resumed $($entry.Task)"
}
