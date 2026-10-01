param([switch]$Check)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$local = Join-Path $repoRoot '.local'
$maintenance = Join-Path $local 'maintenance.request'
if (-not $Check -and (Test-Path -LiteralPath $maintenance)) {
    Write-Host 'Maintenance is active; the tunnel was not started.'
    return
}
$stopRequest = Join-Path $local 'tunnel-stop.request'
if (-not $Check -and (Test-Path -LiteralPath $stopRequest)) {
    # An explicit restart resumes an old stop; a new request during preflight stays intact.
    Remove-Item -LiteralPath $stopRequest
}
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the project environment first.' }
$config = Join-Path $local 'cloudflared.yml'
$binary = Join-Path $local 'bin\cloudflared.exe'
if (-not (Test-Path -LiteralPath $binary)) {
    $binary = (Get-Command cloudflared -ErrorAction Stop).Source
}
Push-Location $repoRoot
try {
    & $python -m simon.https_setup --check
    if ($LASTEXITCODE -ne 0) { throw 'Apply and validate the HTTPS plan before starting a tunnel.' }
    $plan = Get-Content -LiteralPath (Join-Path $local 'https-plan.json') -Raw | ConvertFrom-Json
    if ($plan.provider -ne 'cloudflare') { throw 'This deployment uses a different HTTPS provider.' }
    & $binary tunnel --config $config ingress validate
    if ($LASTEXITCODE -ne 0) { throw 'Tunnel configuration is invalid.' }
    if ($Check) { return }
    if ((Test-Path -LiteralPath $stopRequest) -or (Test-Path -LiteralPath $maintenance)) {
        Write-Host 'A stop or maintenance request arrived during preflight; the tunnel was not started.'
        return
    }
    $logDirectory = Join-Path $local 'logs'
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $log = Join-Path $logDirectory 'cloudflared.log'
    if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 10MB) {
        Move-Item -LiteralPath $log -Destination ($log + '.1') -Force
    }
    # Only this owned child process is stopped; credentials stay in their private file.
    $tunnel = Start-Process -FilePath $binary -ArgumentList @(
        'tunnel', '--config', ('"' + $config + '"'), '--logfile', ('"' + $log + '"'),
        'run', $plan.cloudflared_config.tunnel
    ) -PassThru -WindowStyle Hidden
    try {
        while (-not $tunnel.HasExited) {
            if ((Test-Path -LiteralPath $stopRequest) -or (Test-Path -LiteralPath $maintenance)) {
                Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue
                $tunnel.WaitForExit()
                return
            }
            Start-Sleep -Milliseconds 500
            $tunnel.Refresh()
        }
        throw 'The HTTPS tunnel exited. Check .local/logs/cloudflared.log.'
    } finally {
        if (-not $tunnel.HasExited) { Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue }
        $tunnel.Dispose()
    }
} finally { Pop-Location }
