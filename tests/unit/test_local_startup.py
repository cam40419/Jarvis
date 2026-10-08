import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.adapters.memory import InMemoryStore
from simon.domain.identity import Membership, PasswordCredential

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "local_account_preflight", ROOT / "scripts/check_local_account.py"
)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


@pytest.fixture
def account(monkeypatch):
    actor_id, workspace_id = uuid4(), uuid4()
    settings = SimpleNamespace(
        storage_backend="postgres",
        account_admin_actor_id=actor_id,
        account_workspace_id=workspace_id,
        database_url=SimpleNamespace(get_secret_value=lambda: "synthetic-database"),
    )
    store = InMemoryStore()
    store.put_membership(Membership(actor_id=actor_id, workspace_id=workspace_id, role="owner"))
    closed = []
    monkeypatch.setattr(store, "close", lambda: closed.append(True))
    monkeypatch.setattr(preflight, "Settings", lambda: settings)
    monkeypatch.setattr(preflight, "PostgresStore", lambda _: store)
    return settings, store, closed


def test_config_check_does_not_open_database(account, monkeypatch):
    monkeypatch.setattr(
        preflight, "PostgresStore", lambda _: pytest.fail("Configuration check opened a store")
    )
    assert preflight.main(["--config-only"]) == 0


def test_configured_administrator_accepts_any_enrolled_username(account):
    settings, store, closed = account
    store.save_password(
        PasswordCredential(
            actor_id=settings.account_admin_actor_id,
            username="another-admin",
            password_hash="synthetic-hash",
        )
    )
    assert preflight.main([]) == 0
    assert closed == [True]


def test_administrator_preflight_closes_store_when_not_enrolled(account):
    assert preflight.main([]) == 2
    assert account[2] == [True]


def test_preflight_redacts_invalid_configuration(account, monkeypatch, capsys):
    def invalid():
        raise ValueError("provider-secret")

    monkeypatch.setattr(preflight, "Settings", invalid)
    assert preflight.main(["--config-only"]) == 2
    assert "provider-secret" not in capsys.readouterr().out


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("failed", [False, True])
def test_local_launcher_preserves_configuration_and_restores_explicit_overrides(
    tmp_path, override, failed
):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell unavailable")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "start-local.ps1"
    source = (ROOT / "scripts/start-local.ps1").read_text(encoding="utf-8")
    # Replace only the executable boundary in the isolated copy. No actual Python
    # configuration, database, Docker or installed services are accessed by the fixture.
    original = "$python = Join-Path $repoRoot 'venv\\Scripts\\python.exe'"
    assert source.count(original) == 1
    launcher.write_text(
        source.replace(original, "$python = Join-Path $PSScriptRoot 'fixture-python.ps1'"),
        encoding="utf-8",
    )
    keys = (
        "SIMON_DATABASE_URL",
        "SIMON_ACCOUNT_WORKSPACE_ID",
        "SIMON_ACCOUNT_ADMIN_ACTOR_ID",
        "SIMON_MODEL_PROVIDER",
        "SIMON_PUBLIC_ORIGIN",
        "SIMON_ENVIRONMENT",
    )
    baseline = dict(
        zip(
            keys,
            (
                "postgresql://fixture@localhost/custom",
                str(uuid4()),
                str(uuid4()),
                "local",
                "https://fixture.example",
                "production",
            ),
            strict=True,
        )
    )
    overrides = {
        "SIMON_DATABASE_URL": "postgresql://fixture@localhost/explicit",
        "SIMON_ACCOUNT_WORKSPACE_ID": str(uuid4()),
        "SIMON_ACCOUNT_ADMIN_ACTOR_ID": str(uuid4()),
    }
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps({"baseline": baseline, "overrides": overrides, "override": override}),
        encoding="utf-8",
    )
    capture = tmp_path / "capture.json"
    (scripts / "fixture-python.ps1").write_text(
        "$current = @{}\n"
        "foreach ($key in $fixture.baseline.psobject.Properties.Name) {\n"
        "  $current[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')\n}\n"
        "@{ environment=$current; arguments=@($args) } | ConvertTo-Json -Compress |\n"
        "  Set-Content -LiteralPath $CapturePath -Encoding UTF8\n"
        f"$global:LASTEXITCODE = {2 if failed else 0}\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "run.ps1"
    wrapper.write_text(
        "param($FixturePath,$LauncherPath,$CapturePath)\n$ErrorActionPreference='Stop'\n"
        "$fixture = Get-Content -LiteralPath $FixturePath -Raw | ConvertFrom-Json\n"
        "foreach ($entry in $fixture.baseline.psobject.Properties) {\n"
        "  [Environment]::SetEnvironmentVariable($entry.Name, $entry.Value, 'Process')\n}\n"
        "$options = @{ Check=$true }\n"
        "if ($fixture.override) {\n"
        "  $options.DatabaseUrl=$fixture.overrides.SIMON_DATABASE_URL\n"
        "  $options.WorkspaceId=$fixture.overrides.SIMON_ACCOUNT_WORKSPACE_ID\n"
        "  $options.ActorId=$fixture.overrides.SIMON_ACCOUNT_ADMIN_ACTOR_ID\n}\n"
        "try { & $LauncherPath @options 6>$null | Out-Null; $success=$true }\n"
        "catch { $success=$false }\n"
        "$restored=@{}\n"
        "foreach ($key in $fixture.baseline.psobject.Properties.Name) {\n"
        "  $restored[$key]=[Environment]::GetEnvironmentVariable($key, 'Process')\n}\n"
        "@{ success=$success; restored=$restored } | ConvertTo-Json -Compress\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            str(fixture),
            str(launcher),
            str(capture),
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) == {"success": not failed, "restored": baseline}
    observed = json.loads(capture.read_text(encoding="utf-8-sig"))
    assert observed["environment"] == (baseline | overrides if override else baseline)
    assert observed["arguments"] == ["scripts/check_local_account.py", "--config-only"]
    assert not (tmp_path / ".local").exists()
