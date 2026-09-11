import pytest
from pydantic import SecretStr

from jarvis.config import Settings


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
            "public_origin": "https://jarvis.example",
            "rp_id": "jarvis.example",
        },
        {
            "environment": "production",
            "storage_backend": "memory",
            "public_origin": "https://jarvis.example",
            "rp_id": "jarvis.example",
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
        public_origin="https://jarvis.example",
        rp_id="jarvis.example",
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
    session = next(cookie for cookie in client.cookies.jar if cookie.name == "jarvis_session")
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

    from jarvis.api.app import AppContainer, create_app

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
            if value.startswith("__Host-jarvis_session=")
        )
        for flag in ["Secure", "HttpOnly", "Path=/", "SameSite=strict"]:
            assert flag in cookie
        assert "Domain=" not in cookie
        assert client.get("/auth/session").status_code == 200
