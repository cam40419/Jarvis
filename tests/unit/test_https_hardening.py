"""Independent HTTPS edge and launcher failure-path regressions; no services launched."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from simon.api.app import AppContainer, create_app
from simon.config import Settings
from simon.https_setup import apply_https_plan, https_plan, validate_applied_https

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def configured_https(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("SIMON_", "JARVIS_")):
            monkeypatch.delenv(name)
    local = tmp_path / ".local"
    local.mkdir()
    (local / "maintenance.request").touch()
    (tmp_path / ".env").write_text(
        "SIMON_ENVIRONMENT=development\nSIMON_MODEL_PROVIDER=local\n"
        "SIMON_STORAGE_BACKEND=postgres\nSIMON_PUBLIC_ORIGIN=http://localhost:8000\n"
        "SIMON_DATABASE_URL=postgresql://test:synthetic@localhost/test\n"
        "SIMON_RP_ID=localhost\nSIMON_PUBLIC_PATH=\n",
        encoding="utf-8",
    )
    credentials = tmp_path / "tunnel.json"
    tunnel_id = str(uuid4())
    credentials.write_text(json.dumps({"TunnelID": tunnel_id, "TunnelSecret": "synthetic"}))
    plan = https_plan(
        "https://simon.example.com",
        provider="cloudflare",
        public_path="/simon",
        tunnel_id=tunnel_id,
        credentials_file=credentials,
    )
    apply_https_plan(tmp_path, plan)
    return tmp_path, credentials


@pytest.mark.parametrize("problem", ["missing", "replaced", "large", "type", "directory", "binary"])
def test_proxy_launch_preflight_rechecks_credentials_without_reflecting_content(
    configured_https,
    problem,
):
    root, credentials = configured_https
    assert validate_applied_https(root)["provider"] == "cloudflare"
    if problem in {"missing", "directory"}:
        credentials.unlink()
        if problem == "directory":
            credentials.mkdir()
    elif problem == "replaced":
        credentials.write_text(json.dumps({"TunnelID": str(uuid4()), "TunnelSecret": "secret"}))
    elif problem == "large":
        credentials.write_text("private-content-must-not-appear" * 1000)
    elif problem == "type":
        credentials.write_text(json.dumps(["private-content-must-not-appear"]))
    else:
        credentials.write_bytes(b"\xff\xfe\xff")
    with pytest.raises(ValueError, match="Tunnel credentials") as raised:
        validate_applied_https(root)
    assert "private-content-must-not-appear" not in str(raised.value)


def test_proxy_launch_preflight_rejects_linked_credentials(configured_https):
    root, credentials = configured_https
    original = credentials.read_bytes()
    credentials.unlink()
    target = root / "other-tunnel.json"
    target.write_bytes(original)
    try:
        credentials.symlink_to(target)
    except OSError:
        pytest.skip("Creating symbolic links is unavailable")
    with pytest.raises(ValueError, match="Tunnel credentials"):
        validate_applied_https(root)


@pytest.mark.parametrize("prefix", ["", "/simon"])
def test_outer_ingress_failures_receive_security_headers_and_preserve_auth_caps(prefix):
    settings = Settings(
        environment="test",
        storage_backend="memory",
        model_provider="local",
        public_origin="https://simon.example.com",
        rp_id="simon.example.com",
        public_path=prefix,
        auth_rate_limit=1,
    )
    with TestClient(
        create_app(AppContainer(settings=settings)), base_url="http://simon.example.com"
    ) as client:
        oversized = client.post(prefix + "/auth/password/login", content=b"x" * 65537)
        assert oversized.status_code == 413
        throttled = client.post(prefix + "/auth/passkeys/login/options", json={})
        assert throttled.status_code == 429
        for response in (oversized, throttled):
            assert response.headers["strict-transport-security"] == "max-age=31536000"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["cache-control"] == "no-store"
        assert client.get(prefix + "/login").status_code == 200


@pytest.mark.parametrize("mode", ["stop_during_preflight", "maintenance_during_preflight", "check"])
def test_tunnel_preflight_never_discards_new_stop_or_maintenance_request(tmp_path, mode):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell unavailable")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copied = scripts / "start-tunnel.ps1"
    shutil.copyfile(ROOT / "scripts" / copied.name, copied)
    local = tmp_path / ".local"
    (local / "bin").mkdir(parents=True)
    (local / "bin" / "cloudflared.exe").touch()
    python = tmp_path / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    (local / "tunnel-stop.request").touch()
    (local / "https-plan.json").write_text(
        json.dumps(
            {
                "provider": "cloudflare",
                "cloudflared_config": {"tunnel": str(uuid4())},
            }
        )
    )
    wrapper = tmp_path / "verify.ps1"
    wrapper.write_text(
        "param([string]$Root, [string]$Mode)\n$ErrorActionPreference = 'Stop'\n"
        "$local = Join-Path $Root '.local'\n"
        "$stopRequest = Join-Path $local 'tunnel-stop.request'\n"
        "$python = Join-Path $Root 'venv\\Scripts\\python.exe'\n"
        "$binary = Join-Path $local 'bin\\cloudflared.exe'\n"
        "$observations = @{}\n"
        "Set-Item -LiteralPath ('Function:\\' + $python) -Value {\n"
        "  $observations['oldMarkerPresentAtCheck'] = Test-Path -LiteralPath $stopRequest\n"
        "  if ($Mode -eq 'stop_during_preflight') {\n"
        "    Set-Content -LiteralPath $stopRequest -Value 'new request'\n"
        "  } elseif ($Mode -eq 'maintenance_during_preflight') {\n"
        "    Set-Content -LiteralPath (Join-Path $local 'maintenance.request') "
        "-Value 'maintenance'\n"
        "  }\n  $global:LASTEXITCODE = 0\n}\n"
        "Set-Item -LiteralPath ('Function:\\' + $binary) -Value { $global:LASTEXITCODE = 0 }\n"
        "function Start-Process { throw 'A tunnel must never launch in this test' }\n"
        "$script = Join-Path $Root 'scripts\\start-tunnel.ps1'\n"
        "if ($Mode -eq 'check') { & $script -Check | Out-Null } else { & $script | Out-Null }\n"
        "$observations['stopStillPresent'] = Test-Path -LiteralPath $stopRequest\n"
        "$observations['maintenance'] = Test-Path -LiteralPath "
        "(Join-Path $local 'maintenance.request')\n"
        "$observations | ConvertTo-Json -Compress\n",
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
            str(tmp_path),
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    observed = json.loads(result.stdout.splitlines()[-1])
    assert observed["oldMarkerPresentAtCheck"] == (mode == "check")
    assert observed["stopStillPresent"] == (mode != "maintenance_during_preflight")
    assert observed["maintenance"] == (mode == "maintenance_during_preflight")
