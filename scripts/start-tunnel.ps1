param([switch]$Check)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$config = Join-Path $repoRoot '.local\cloudflared.yml'
$binary = Join-Path $repoRoot '.local\bin\cloudflared.exe'
if (-not (Test-Path -LiteralPath $binary)) {
    $binary = (Get-Command cloudflared -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath $config)) {
    throw 'Set up .local/cloudflared.yml using docs/runbooks/remote-voice.md first.'
}
& $binary tunnel --config $config ingress validate
if ($LASTEXITCODE -ne 0) { throw 'Tunnel configuration is invalid.' }
if ($Check) { return }
& $binary tunnel --config $config run simon
if ($LASTEXITCODE -ne 0) { throw 'Simon tunnel exited unexpectedly.' }
