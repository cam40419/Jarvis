from time import time
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from pydantic import SecretStr

from simon.adapters.google import CALENDAR_SCOPE, EMAIL_SCOPE, GoogleTokens
from simon.services.connected import ConnectedService
from tests.contract.test_connected import make_proposal


def test_google_connection_callback_and_confirm_api(client, container, auth_headers):
    settings = container.settings.model_copy(
        update={
            "google_client_id": "test-client",
            "google_client_secret": SecretStr("secret"),
            "google_token_key": SecretStr(Fernet.generate_key().decode()),
        }
    )
    service = ConnectedService(container.store, container.audit, settings, container.identity)
    container.connected = service
    tokens = GoogleTokens(
        access_token="access-secret", refresh_token="refresh-secret", expires_at=time() + 3600
    )
    service.api.exchange = lambda *args: (tokens, (CALENDAR_SCOPE, EMAIL_SCOPE))
    service.api.account_email = lambda *args: "owner@example.com"
    path = "/v1/connections/google/start"
    assert client.post(path, json={"shared_chat_acknowledged": True}).status_code == 403
    assert (
        client.post(
            path, headers=auth_headers, json={"shared_chat_acknowledged": False}
        ).status_code
        == 422
    )
    response = client.post(path, headers=auth_headers, json={"shared_chat_acknowledged": True})
    assert response.status_code == 200
    assert (
        "HttpOnly" in response.headers["set-cookie"]
        and "SameSite=lax" in response.headers["set-cookie"]
    )
    state = parse_qs(urlsplit(response.json()["url"]).query)["state"][0]
    # The regular Strict session cookie need not accompany the cross-site callback.
    session = client.cookies.get("simon_session")
    client.cookies.delete("simon_session")
    callback = client.get(
        "/auth/google/callback",
        params={"state": state, "code": "secret-code"},
        follow_redirects=False,
    )
    assert callback.status_code == 303 and callback.headers["location"] == "/chat?google=connected"
    assert (
        "secret-code" not in callback.text and callback.headers["Referrer-Policy"] == "no-referrer"
    )
    client.cookies.set("simon_session", session)
    status = client.get("/v1/connections/google").json()
    assert status["email"] == "owner@example.com" and status["calendar"] and status["email_send"]
    assert "secret" not in str(status)
    assert (
        client.get(
            "/auth/google/callback",
            params={"state": state, "code": "reuse"},
            follow_redirects=False,
        ).headers["location"]
        == "/chat?google=failed"
    )
    _, actor = container.identity.resolve(session)
    action, _, _ = make_proposal(service, actor)
    calls = []
    service.api.execute = lambda *args: (calls.append(1) or "receipt", None)
    assert client.get(f"/v1/actions/{action.id}").json()["status"] == "pending"
    assert client.post(f"/v1/actions/{action.id}/confirm", json={}).status_code == 403
    response = client.post(f"/v1/actions/{action.id}/confirm", headers=auth_headers, json={})
    assert response.json()["status"] == "succeeded" and calls == [1]
    assert (
        client.post(f"/v1/actions/{action.id}/confirm", headers=auth_headers, json={}).json()
        == response.json()
    )
    action, _, _ = make_proposal(service, actor, "propose_calendar_event")
    assert (
        client.post(f"/v1/actions/{action.id}/cancel", headers=auth_headers, json={}).json()[
            "status"
        ]
        == "cancelled"
    )
    assert calls == [1]
    assert (
        client.post("/v1/connections/google/disconnect", headers=auth_headers, json={}).status_code
        == 204
    )
    assert not client.get("/v1/connections/google").json()["connected"]


def test_unconfigured_google_is_actionable_and_unauthenticated_is_denied(client, auth_headers):
    assert not client.get("/v1/connections/google").json()["configured"]
    response = client.post(
        "/v1/connections/google/start",
        headers=auth_headers,
        json={"shared_chat_acknowledged": True},
    )
    assert response.status_code == 400 and "setup guide" in response.json()["error"]["message"]
    client.cookies.clear()
    assert client.get("/v1/connections/google").status_code == 401
