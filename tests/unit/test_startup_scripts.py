import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_startup", ROOT / "scripts/check_startup.py")
assert SPEC and SPEC.loader
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)


@pytest.fixture
def configuration(monkeypatch):
    settings = SimpleNamespace(
        storage_backend="postgres", environment="production", agent_execution_enabled=True,
        agent_manifest_file="configured.json",
    )
    monkeypatch.setattr(startup, "Settings", lambda: settings)
    monkeypatch.setattr(
        startup, "load_manifest", lambda path: SimpleNamespace(teams=["configured"]),
    )
    return settings


@pytest.mark.parametrize("service", ["server", "agents"])
def test_preflight_validates_settings_without_starting_services(configuration, service, capsys):
    assert startup.main([service]) == 0
    assert "validated" in capsys.readouterr().out


@pytest.mark.parametrize(("service", "changes"), [
    ("server", {"storage_backend": "memory"}),
    ("agents", {"storage_backend": "memory"}),
    ("server", {"environment": "development"}),
    ("agents", {"agent_execution_enabled": False}),
])
def test_preflight_rejects_unready_settings(configuration, service, changes):
    for key, value in changes.items():
        setattr(configuration, key, value)
    assert startup.main([service]) == 2


def test_preflight_requires_manifest_and_redacts_parser_errors(configuration, monkeypatch, capsys):
    monkeypatch.setattr(startup, "load_manifest", lambda path: SimpleNamespace(teams=[]))
    assert startup.main(["agents"]) == 2

    def invalid():
        raise ValueError("settings input_value=provider-secret")

    monkeypatch.setattr(startup, "Settings", invalid)
    assert startup.main(["server"]) == 2
    assert "provider-secret" not in capsys.readouterr().out


def test_powershell_launchers_parse_without_executing_or_registering_tasks(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed")
    # Pass paths as positional file arguments: no shell interpolation of checkout paths.
    parser = tmp_path / "parse.ps1"
    parser.write_text(
        "$rows = foreach ($path in $args) {\n"
        "  $tokens = $null; $errors = $null\n"
        "  [System.Management.Automation.Language.Parser]::ParseFile(\n"
        "    $path, [ref]$tokens, [ref]$errors) | Out-Null\n"
        "  [pscustomobject]@{ name = [IO.Path]::GetFileName($path); errors = @($errors).Count }\n"
        "}\nConvertTo-Json -InputObject @($rows) -Compress\n",
        encoding="utf-8",
    )
    scripts = [ROOT / "scripts" / name for name in (
        "start-agent-dispatcher.ps1", "install-agent-task.ps1", "start-configured-server.ps1",
        "start-tunnel.ps1", "stop-tunnel.ps1", "install-https-tasks.ps1",
        "resume-local.ps1", "install-local-tasks.ps1",
    )]
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(parser), *(str(path) for path in scripts)],
        capture_output=True, text=True, check=True, timeout=20,
    )
    rows = json.loads(result.stdout)
    assert len(rows) == len(scripts) and all(row["errors"] == 0 for row in rows)


@pytest.mark.parametrize(("script", "marker"), [
    ("start-agent-dispatcher.ps1", "agent-dispatcher-stop.request"),
    ("start-configured-server.ps1", "simon-stop.request"),
])
@pytest.mark.parametrize("mode", ["maintenance", "check", "restart"])
def test_launchers_honor_maintenance_and_reset_marker_only_on_restart(
    tmp_path, script, marker, mode,
):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copied = scripts / script
    shutil.copyfile(ROOT / "scripts" / script, copied)
    local = tmp_path / ".local"
    local.mkdir()
    stop_file = local / marker
    stop_file.touch()
    if mode != "restart":
        (local / "maintenance.request").touch()
    arguments = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(copied)]
    if mode == "check":
        arguments.append("-Check")
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=20, check=False)
    if mode == "maintenance":
        assert result.returncode == 0 and "Maintenance is active" in result.stdout
    else:
        # No environment is installed in this isolated fixture. Reaching this
        # guard proves startup/check semantics without running any actual service.
        assert result.returncode != 0 and "Install the project environment" in result.stderr
    assert stop_file.exists() == (mode != "restart")


@pytest.mark.parametrize(("script", "marker"), [
    ("start-agent-dispatcher.ps1", "agent-dispatcher-stop.request"),
    ("start-assistant-worker.ps1", "assistant-worker-stop.request"),
    ("start-workflow-worker.ps1", "assistant-worker-stop.request"),
    ("start-local.ps1", "simon-stop.request"),
    ("start-configured-server.ps1", "simon-stop.request"),
])
def test_scheduled_logon_or_retry_preserves_deliberate_stop(tmp_path, script, marker):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in {script, "start-assistant-worker.ps1"}:
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    local = tmp_path / ".local"
    local.mkdir()
    stop = local / marker
    stop.touch()
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(scripts / script), "-Supervised"],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0 and "stop request is active" in result.stdout
    assert stop.exists() and not (local / "logs").exists()


@pytest.mark.parametrize(("state", "maintenance", "resume"), [
    ("Ready", False, True), ("Ready", True, False), ("Running", False, False),
    ("Disabled", False, False), (None, False, False),
])
def test_explicit_resume_clears_only_selected_stopped_service(tmp_path, state, maintenance, resume):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copied = scripts / "resume-local.ps1"
    shutil.copyfile(ROOT / "scripts" / copied.name, copied)
    local = tmp_path / ".local"
    local.mkdir()
    stop = local / "agent-dispatcher-stop.request"
    other = local / "assistant-worker-stop.request"
    stop.touch()
    other.touch()
    if maintenance:
        (local / "maintenance.request").touch()
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"state": state}), encoding="utf-8")
    wrapper = tmp_path / "run.ps1"
    wrapper.write_text(
        "param($FixturePath,$ResumePath)\n$ErrorActionPreference = 'Stop'\n"
        "$fixture = Get-Content -LiteralPath $FixturePath -Raw | ConvertFrom-Json\n"
        "$started = New-Object 'System.Collections.Generic.List[string]'\n"
        "function Get-ScheduledTask { param($TaskName,$ErrorAction)\n"
        "  if ($fixture.state) { [pscustomobject]@{ State = $fixture.state } }\n}\n"
        "function Start-ScheduledTask { param($TaskName) $started.Add($TaskName) }\n"
        "try { & $ResumePath -Service agents 6>$null | Out-Null; $success = $true }\n"
        "catch { $success = $false }\n"
        "@{ success = $success; started = @($started.ToArray()) } | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
         str(wrapper), str(fixture), str(copied)],
        capture_output=True, text=True, timeout=20, check=True,
    )
    report = json.loads(result.stdout)
    assert report == {"success": resume, "started": ["Simon-Agents"] if resume else []}
    assert stop.exists() is not resume and other.exists()
