import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from simon.api.app import AppContainer, create_app
from simon.config import Settings
from simon.https_setup import apply_https_plan, https_plan, validate_applied_https


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("SIMON_", "JARVIS_")):
            monkeypatch.delenv(key)
    local = tmp_path / ".local"
    local.mkdir()
    (local / "maintenance.request").touch()
    (tmp_path / ".env").write_text(
        "# Preserve operator configuration\nSIMON_ENVIRONMENT=development\n"
        "SIMON_MODEL_PROVIDER=local\nSIMON_STORAGE_BACKEND=postgres\n"
        "SIMON_DATABASE_URL=postgresql://test:synthetic@localhost/test\n"
        "SIMON_PUBLIC_ORIGIN=http://localhost:8000\nSIMON_RP_ID=localhost\n"
        "SIMON_PUBLIC_PATH=\nSIMON_OPENAI_API_KEY=synthetic-private-key\n",
        encoding="utf-8",
    )
    credentials = tmp_path / "private-tunnel.json"
    tunnel_id = str(uuid4())
    credentials.write_text(json.dumps({"TunnelID": tunnel_id, "TunnelSecret": "synthetic"}))
    plan = https_plan("https://simon.example.com", provider="cloudflare", public_path="/simon",
                      tunnel_id=tunnel_id, credentials_file=credentials)
    return tmp_path, plan


@pytest.mark.parametrize("origin", [
    "http://simon.example.com", "https://localhost", "https://127.0.0.1",
    "https://[::1]", "https://simon.local", "https://simon.example.com/",
    "https://simon.example.com:443", "https://user@simon.example.com",
    "https://simon.example.com?x=1", "https://simon.example.com#x",
    "https://Simon.example.com", "https://foo..example.com", "https://-foo.example.com",
])
def test_https_origin_is_exact_dns_origin(origin):
    with pytest.raises(ValueError):
        https_plan(origin, provider="tailscale")


def test_cloudflare_plan_keeps_proxy_loopback_and_host_path_consistent(deployment):
    _, plan = deployment
    ingress = plan["cloudflared_config"]["ingress"]
    assert ingress == [{"hostname": "simon.example.com", "service": "http://127.0.0.1:8000",
                        "originRequest": {"httpHostHeader": "simon.example.com"},
                        "path": "^/simon(?:/.*)?$"}, {"service": "http_status:404"}]
    assert plan["google_callback_url"] == "https://simon.example.com/simon/auth/google/callback"
    assert plan["local_health_url"] == "http://127.0.0.1:8000/simon/health/live"
    assert "synthetic" not in json.dumps(plan)


def test_tailscale_plan_uses_private_serve_and_requires_device_hostname():
    plan = https_plan("https://simon.example.ts.net", provider="tailscale")
    assert plan["serve_argv"] == ["tailscale", "serve", "--bg", "--https=443",
                                  "http://127.0.0.1:8000"]
    assert "cloudflared_config" not in plan
    with pytest.raises(ValueError):
        https_plan("https://simon.example.com", provider="tailscale")
    with pytest.raises(ValueError):
        https_plan("https://simon.example.ts.net", provider="tailscale", public_path="/bad/../path")


def test_apply_preserves_credentials_and_retains_exact_private_rollback(deployment):
    root, plan = deployment
    original = (root / ".env").read_bytes()
    backup = apply_https_plan(root, plan)
    assert (backup / "server.env").read_bytes() == original
    after = dotenv_values(root / ".env")
    assert after["SIMON_OPENAI_API_KEY"] == "synthetic-private-key"
    assert after["SIMON_DATABASE_URL"] == "postgresql://test:synthetic@localhost/test"
    assert all(after[key] == value for key, value in plan["environment_updates"].items())
    assert validate_applied_https(root) == plan
    (root / ".local" / "cloudflared.yml").write_text("{}")
    with pytest.raises(ValueError, match="proxy configuration"):
        validate_applied_https(root)


@pytest.mark.parametrize("problem", ["maintenance", "credentials", "tampered"])
def test_apply_refuses_unsafe_configuration_without_modifying_env(deployment, problem):
    root, plan = deployment
    original = (root / ".env").read_bytes()
    if problem == "maintenance":
        (root / ".local" / "maintenance.request").unlink()
    elif problem == "credentials":
        Path(plan["cloudflared_config"]["credentials-file"]).write_text(
            json.dumps({"TunnelID": str(uuid4()), "TunnelSecret": "secret-must-not-appear"}),
        )
    else:
        plan["environment_updates"]["SIMON_OPENAI_API_KEY"] = "inject"
    with pytest.raises(ValueError) as raised:
        apply_https_plan(root, plan)
    assert "secret-must-not-appear" not in str(raised.value)
    assert (root / ".env").read_bytes() == original
    assert not (root / ".local" / "https-backups").exists()


def test_configured_https_headers_and_redirect_ignore_forwarded_origin():
    settings = Settings(environment="test", storage_backend="memory", model_provider="local",
                        public_origin="https://simon.example.com", rp_id="simon.example.com")
    with TestClient(create_app(AppContainer(settings=settings)),
                    base_url="http://simon.example.com") as client:
        response = client.get("/login", headers={"X-Forwarded-Proto": "http"})
        assert response.status_code == 200
        assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
        redirect = client.get("/login/", follow_redirects=False,
                              headers={"X-Forwarded-Host": "attacker.example"})
        assert redirect.headers["location"] == "https://simon.example.com/login"
        redirected_port = client.get("/login/", follow_redirects=False,
                                     headers={"Host": "simon.example.com:1234"})
        assert redirected_port.headers["location"] == "https://simon.example.com/login"
        for malformed in ("simon.example.com:abc", "simon.example.com:80@attacker.example", "["):
            assert client.get("/login/", headers={"Host": malformed}).status_code == 400
        assert client.get("/login", headers={"Host": "attacker.example"}).status_code == 400


def test_localhost_does_not_receive_hsts(client):
    assert "strict-transport-security" not in client.get("/login").headers


def test_https_prefix_redirect_and_unmatched_paths_are_protected():
    settings = Settings(environment="test", storage_backend="memory", model_provider="local",
                        public_origin="https://simon.example.com", rp_id="simon.example.com",
                        public_path="/simon")
    with TestClient(create_app(AppContainer(settings=settings)),
                    base_url="http://simon.example.com") as client:
        redirect = client.get("/simon", follow_redirects=False)
        assert redirect.headers["location"] == "https://simon.example.com/simon/"
        assert "strict-transport-security" in redirect.headers
        assert client.get("/elsewhere").headers["x-content-type-options"] == "nosniff"
        assert client.get("/simon", headers={"Host": "attacker.example"}).status_code == 400


def test_apply_rolls_back_proxy_and_plan_if_env_replacement_fails(deployment, monkeypatch):
    root, plan = deployment
    before = (root / ".env").read_bytes()
    proxy = root / ".local" / "cloudflared.yml"
    proxy.write_text("old-proxy-config", encoding="utf-8")
    replace = os.replace

    def fail_env(source, target):
        if target == root / ".env":
            raise OSError("synthetic disk failure")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", fail_env)
    with pytest.raises(OSError):
        apply_https_plan(root, plan)
    assert (root / ".env").read_bytes() == before
    assert proxy.read_text(encoding="utf-8") == "old-proxy-config"
    assert not (root / ".local" / "https-plan.json").exists()
