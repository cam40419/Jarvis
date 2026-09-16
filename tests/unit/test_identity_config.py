import pytest
from pydantic import SecretStr

from simon.config import Settings


def test_openai_key_is_simon_only_and_other_legacy_settings_still_work(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("JARVIS_OPENAI_API_KEY=old-synthetic-key\nJARVIS_AUTO_DEEP_ENABLED=false\n")
    legacy = Settings(_env_file=env)
    assert legacy.openai_api_key is None
    assert legacy.auto_deep_enabled is False
    monkeypatch.setenv("SIMON_OPENAI_API_KEY", "new-synthetic-key")
    monkeypatch.setenv("SIMON_AUTO_DEEP_ENABLED", "true")
    modern = Settings(_env_file=env)
    assert modern.openai_api_key.get_secret_value() == "new-synthetic-key"
    assert modern.auto_deep_enabled is True


def test_legacy_process_key_cannot_shadow_simon_dotenv_key(monkeypatch, tmp_path):
    monkeypatch.delenv("SIMON_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_OPENAI_API_KEY", "old-process-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-sdk-secret")
    env = tmp_path / ".env"
    env.write_text("SIMON_OPENAI_API_KEY=new-file-secret\n")
    settings = Settings(_env_file=env, model_provider="openai")
    assert settings.openai_api_key.get_secret_value() == "new-file-secret"
    assert "new-file-secret" not in repr(settings)


def test_legacy_only_key_reports_the_simon_setting(monkeypatch, tmp_path):
    monkeypatch.delenv("SIMON_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_OPENAI_API_KEY", "old-process-secret")
    with pytest.raises(ValueError, match="SIMON_OPENAI_API_KEY"):
        Settings(_env_file=None, model_provider="openai")


@pytest.mark.parametrize(
    "changes",
    [
        {"public_origin": "http://localhost:8000/path"},
        {"public_origin": "http://localhost:8000?query=1"},
        {"public_origin": "http://other.example", "rp_id": "other.example"},
        {"rp_id": "other.example"},
        {"dev_login_enabled": True},
        {"dev_login_enabled": True, "dev_login_token": SecretStr("short")},
        {
            "dev_login_enabled": True,
            "dev_login_token": SecretStr("a" * 32),
            "public_origin": "https://simon.example",
            "rp_id": "simon.example",
        },
        {
            "environment": "production",
            "storage_backend": "memory",
            "public_origin": "https://simon.example",
            "rp_id": "simon.example",
        },
    ],
)
def test_identity_configuration_fails_closed(changes):
    with pytest.raises(ValueError):
        Settings(**changes)


def test_production_requires_persistent_secure_identity():
    settings = Settings(
        environment="production",
        storage_backend="postgres",
        public_origin="https://simon.example",
        rp_id="simon.example",
    )
    assert settings.secure_cookies
    assert not settings.dev_login_enabled


def test_signin_page_and_cookie_security(client, auth_headers):
    page = client.get("/login")
    assert page.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert client.get("/").url.path == "/login"
    assert client.get("/assets/login.js").status_code == 200
    assert client.get("/auth/config").json()["dev_login_enabled"] is True
    assert client.get("/health/live", headers={"Host": "evil.example"}).status_code == 400
    session = next(cookie for cookie in client.cookies.jar if cookie.name == "simon_session")
    assert session.has_nonstandard_attr("HttpOnly")
    assert session.get_nonstandard_attr("SameSite") == "strict"
    assert page.headers["Cache-Control"] == "no-store"


def test_login_does_not_echo_invalid_secrets(client):
    secret = "sensitive-example-" * 30
    response = client.post(
        "/auth/dev-login", headers={"Origin": "http://localhost:8000"}, json={"token": secret}
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert (
        client.post(
            "/auth/dev-login", headers={"Origin": "http://localhost:8000"}, json={"token": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/passkeys/login/options", headers={"Origin": "http://evil.example"}
        ).status_code
        == 403
    )


def test_https_cookies_use_secure_host_prefix():
    from fastapi.testclient import TestClient

    from simon.api.app import AppContainer, create_app

    secret = "https-development-secret-32-characters"
    origin = "https://localhost:8443"
    settings = Settings(
        public_origin=origin, dev_login_enabled=True, dev_login_token=SecretStr(secret)
    )
    with TestClient(create_app(AppContainer(settings=settings)), base_url=origin) as client:
        response = client.post(
            "/auth/dev-login", headers={"Origin": origin}, json={"token": secret}
        )
        assert response.status_code == 200
        cookie = next(
            value
            for value in response.headers.get_list("set-cookie")
            if value.startswith("__Host-simon_session=")
        )
        for flag in ["Secure", "HttpOnly", "Path=/", "SameSite=strict"]:
            assert flag in cookie
        assert "Domain=" not in cookie
        assert client.get("/auth/session").status_code == 200
