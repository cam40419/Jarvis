import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TASKS = ("Simon-PostgreSQL", "Simon-Local", "Simon-Workflow", "Simon-Agents", "Simon-Tunnel")


@pytest.mark.parametrize(("database", "application", "state", "markers", "expected"), [
    (True, False, "Ready", (), {"Simon-Local", "Simon-Workflow", "Simon-Agents"}),
    (False, False, "Ready", (), {"Simon-Local", "Simon-PostgreSQL"}),
    (True, True, "Ready", (), {"Simon-Workflow", "Simon-Agents"}),
    (True, False, "Disabled", (), set()),
    (True, False, "Running", (), set()),
    (True, False, "Ready", ("maintenance.request",), set()),
    (True, False, "Ready", ("simon-stop.request", "assistant-worker-stop.request",
                            "agent-dispatcher-stop.request"), set()),
    (True, True, "Ready", ("agent-dispatcher-stop.request",), {"Simon-Workflow"}),
    (True, True, None, (), set()),
])
def test_recovery_respects_health_disabled_tasks_and_stop_markers(
    tmp_path, database, application, state, markers, expected,
):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copied = scripts / "recover-local.ps1"
    shutil.copyfile(ROOT / "scripts" / copied.name, copied)
    local = tmp_path / ".local"
    local.mkdir()
    for marker in markers:
        (local / marker).touch()
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"database": database, "application": application,
                                  "states": dict.fromkeys(TASKS, state)}), encoding="utf-8")
    wrapper = tmp_path / "run.ps1"
    wrapper.write_text(
        "param([string]$FixturePath, [string]$RecoveryPath)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$fixture = Get-Content -LiteralPath $FixturePath -Raw | ConvertFrom-Json\n"
        "$started = New-Object 'System.Collections.Generic.List[string]'\n"
        "function Get-ScheduledTask { param($TaskName, $ErrorAction)\n"
        "  if ($fixture.states.$TaskName) {\n"
        "    [pscustomobject]@{ State = $fixture.states.$TaskName }\n"
        "  }\n}\n"
        "function Start-ScheduledTask { param($TaskName) $started.Add($TaskName) }\n"
        "function docker { $global:LASTEXITCODE = 0\n"
        "  if ($args[0] -eq 'compose') { return 'synthetic-container' }\n"
        "  if ($fixture.database) { 'healthy' } else { 'unhealthy' }\n}\n"
        "function Invoke-WebRequest {\n"
        "  if ($fixture.application) { [pscustomobject]@{ StatusCode = 200 } }\n"
        "  else { throw 'Synthetic unhealthy app' }\n}\n"
        "& $RecoveryPath\n"
        "ConvertTo-Json -InputObject @($started.ToArray()) -Compress\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(wrapper), str(fixture), str(copied)],
        capture_output=True, text=True, timeout=20, check=True,
    )
    assert set(json.loads(result.stdout)) == expected
    assert all((local / marker).exists() for marker in markers)


@pytest.mark.parametrize("stopped", [False, True])
def test_recovery_probes_https_hostname_on_loopback_and_respects_tunnel_stop(tmp_path, stopped):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copied = scripts / "recover-local.ps1"
    shutil.copyfile(ROOT / "scripts" / copied.name, copied)
    local = tmp_path / ".local"
    local.mkdir()
    (local / "https-plan.json").write_text(json.dumps({
        "provider": "cloudflare", "health_host": "simon.example.com",
        "environment_updates": {"SIMON_PUBLIC_PATH": "/simon"},
        "local_health_url": "https://must-never-connect.example/",
    }))
    if stopped:
        (local / "tunnel-stop.request").touch()
    wrapper = tmp_path / "run.ps1"
    wrapper.write_text(
        "param([string]$RecoveryPath)\n$ErrorActionPreference = 'Stop'\n"
        "$started = New-Object 'System.Collections.Generic.List[string]'\n"
        "$observedHealth = @{}\n"
        "function Get-ScheduledTask { param($TaskName, $ErrorAction)\n"
        "  if ($TaskName -eq 'Simon-Tunnel') { [pscustomobject]@{ State = 'Ready' } }\n}\n"
        "function Start-ScheduledTask { param($TaskName) $started.Add($TaskName) }\n"
        "function docker { $global:LASTEXITCODE = 0\n"
        "  if ($args[0] -eq 'compose') { 'test-container' } else { 'healthy' }\n}\n"
        "function Invoke-WebRequest {\n"
        "  param($Uri, $Headers, $TimeoutSec, [switch]$UseBasicParsing)\n"
        "  if ($Uri -ne 'http://127.0.0.1:8000/simon/health/live' -or\n"
        "      $Headers.Host -ne 'simon.example.com') { throw 'Wrong health target' }\n"
        "  $observedHealth['correct'] = $true\n  [pscustomobject]@{ StatusCode = 200 }\n}\n"
        "& $RecoveryPath\n"
        "@{ started = @($started.ToArray()); healthy = $observedHealth.correct } "
        "| ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(wrapper), str(copied)], capture_output=True, text=True, timeout=20, check=True,
    )
    result_json = json.loads(result.stdout)
    assert result_json["healthy"]
    assert result_json["started"] == ([] if stopped else ["Simon-Tunnel"])


def test_changed_powershell_scripts_parse_without_service_actions(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    parser = tmp_path / "parse.ps1"
    parser.write_text(
        "foreach ($path in $args) {\n"
        "  $tokens = $null; $errors = $null\n"
        "  [System.Management.Automation.Language.Parser]::ParseFile(\n"
        "    $path, [ref]$tokens, [ref]$errors) | Out-Null\n"
        "  if (@($errors).Count) { throw ($errors | Out-String) }\n}\n",
        encoding="utf-8",
    )
    subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(parser), *(str(ROOT / "scripts" / name) for name in (
             "start-assistant-worker.ps1", "start-workflow-worker.ps1",
             "stop-assistant-worker.ps1", "recover-local.ps1", "start-local.ps1",
        ))], capture_output=True, text=True, timeout=20, check=True,
    )


def test_compatibility_worker_propagates_launcher_failure(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("start-workflow-worker.ps1", "start-assistant-worker.ps1"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(scripts / "start-workflow-worker.ps1")],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode != 0 and "Install the project environment" in result.stderr
