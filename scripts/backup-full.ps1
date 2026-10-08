# Preserve complete local data in an idle maintenance window. Never force-kill a worker.
param([string]$BackupRoot = '', [switch]$Force)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$localRoot = Join-Path $repoRoot '.local'
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
if (-not $BackupRoot) {
    $BackupRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Simon\backups'
}
$BackupRoot = [System.IO.Path]::GetFullPath($BackupRoot)
$logRoot = Join-Path $localRoot 'logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
$statusPath = Join-Path $BackupRoot 'last-success.json'
if (-not $Force -and (Test-Path -LiteralPath $statusPath)) {
    $last = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
    if (([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($last.verified_at)).TotalHours -lt 23) {
        Write-Host 'A full recovery bundle has already been verified today.'
        return
    }
}
$token = 'full-backup-' + [guid]::NewGuid().ToString('N')
$maintenance = Join-Path $localRoot 'maintenance.request'
$services = @(
    @{ Task = 'Simon-Local'; Marker = 'simon-stop.request' },
    @{ Task = 'Simon-Assistant'; Marker = 'assistant-worker-stop.request' }
)
foreach ($entry in $services) {
    if (Test-Path -LiteralPath (Join-Path $localRoot $entry.Marker)) {
        Write-Host 'Full backup deferred; an existing service stop request is preserved.'
        return
    }
}
if (Test-Path -LiteralPath $maintenance) {
    Write-Host 'Full backup deferred; maintenance is already active.'
    return
}
$owned = [System.Collections.Generic.List[string]]::new()
$resume = [System.Collections.Generic.List[object]]::new()
$drained = $false
$created = $false
$transcript = $false
function Own-Marker([string]$Path) {
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($token)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    $owned.Add($Path)
}
Push-Location $repoRoot
try {
    Start-Transcript -Path (Join-Path $logRoot 'backup-full.log') -Append | Out-Null
    $transcript = $true
    Own-Marker $maintenance
    & $python (Join-Path $PSScriptRoot 'backup_idle.py')
    if ($LASTEXITCODE -eq 10) { $drained = $true; return }
    if ($LASTEXITCODE -ne 0) { $drained = $true; throw 'Could not verify the idle backup window.' }
    foreach ($entry in $services) {
        $task = Get-ScheduledTask -TaskName $entry.Task -ErrorAction SilentlyContinue
        if ($task -and $task.State -eq 'Running') {
            Own-Marker (Join-Path $localRoot $entry.Marker)
            $resume.Add($entry)
        }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    do {
        $running = @($services | ForEach-Object {
            Get-ScheduledTask -TaskName $_.Task -ErrorAction SilentlyContinue
        } | Where-Object State -eq 'Running')
        if ($running.Count -eq 0) { $drained = $true; break }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not $drained) {
        throw 'Services are still draining. Backup was not taken; owned stop markers remain for review.'
    }
    $destination = Join-Path $BackupRoot ('simon_full_' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    & $python (Join-Path $PSScriptRoot 'backup_bundle.py') create $destination --writers-stopped
    if ($LASTEXITCODE -ne 0) { throw 'Full recovery bundle creation failed; partial bytes were preserved.' }
    $created = $true
} finally {
    if ($drained) {
        foreach ($marker in $owned) {
            if ((Test-Path -LiteralPath $marker) -and [System.IO.File]::ReadAllText($marker) -eq $token) {
                Remove-Item -LiteralPath $marker
            }
        }
        if (-not (Test-Path -LiteralPath $maintenance)) {
            foreach ($entry in $resume) {
                $task = Get-ScheduledTask -TaskName $entry.Task -ErrorAction SilentlyContinue
                if ($task -and $task.State -ne 'Disabled' -and
                    -not (Test-Path -LiteralPath (Join-Path $localRoot $entry.Marker))) {
                    Start-ScheduledTask -TaskName $entry.Task
                }
            }
        }
    }
    if ($transcript) { Stop-Transcript | Out-Null }
    Pop-Location
}
if ($created) {
    # Restore verification targets a new disposable database; running Simon is untouched.
    Push-Location $repoRoot
    try {
        & $python (Join-Path $PSScriptRoot 'backup_bundle.py') verify $destination --database
        if ($LASTEXITCODE -ne 0) { throw 'The recovery bundle failed its isolated restore check.' }
        $status = @{ verified_at = [DateTimeOffset]::UtcNow.ToString('o'); bundle = $destination;
            database_restored = $true; files_verified = $true; encrypted = $false;
            includes_secrets = $false }
        $partialStatus = $statusPath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
        $status | ConvertTo-Json | Set-Content -LiteralPath $partialStatus -Encoding UTF8
        Move-Item -LiteralPath $partialStatus -Destination $statusPath -Force
        Write-Host 'Full data backup and isolated restore verified. No historical bundles were removed.'
    } finally { Pop-Location }
}
