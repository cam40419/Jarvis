# Recover only an already-started print batch. Staged batches are never started here.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$printerRoot = Join-Path (Split-Path -Parent $repoRoot) 'autoswap_rip'
$python = Join-Path $repoRoot 'venv\Scripts\python.exe'
$bridge = Join-Path $printerRoot 'simon_bridge.py'
$logDir = Join-Path $repoRoot '.local\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
# Native redirection with Continue avoids PowerShell 5 treating diagnostic stderr as fatal.
$ErrorActionPreference = 'Continue'
& $python $bridge recover-batches *>> (Join-Path $logDir 'autoswap-recovery.log')
exit $LASTEXITCODE
