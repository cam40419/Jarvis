from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from simon.api.app import create_app
from simon.services.identity import csrf_token
from tests.contract.test_voice import offer, setup_voice


def test_voice_api_owner_csrf_heartbeat_stop_and_close(client, container, auth_headers):
    service, _actor, token, _, socket = setup_voice(container.store)
    container.voice = service
    # Both IdentityService instances share durable sessions.
    client.cookies.set("simon_session", token)
    csrf = csrf_token(token)
    headers = {"Origin": "http://localhost:8000", "X-CSRF-Token": csrf}
    assert client.get("/v1/voice").json()["enabled"]
    body = offer().model_dump(mode="json")
    assert client.post("/v1/voice/sessions", json=body).status_code == 403
    response = client.post("/v1/voice/sessions", json=body, headers=headers)
    assert response.status_code == 200, response.text
    identifier = response.json()["id"]
    path = "/v1/voice/sessions/" + identifier
    assert "provider" not in response.text and "synthetic-voice-key" not in response.text
    assert client.post("/v1/voice/sessions", json=body, headers=headers).status_code == 409
    assert client.post(path + "/heartbeat", json={}, headers=headers).status_code == 200
    live = service.active[UUID(identifier)]
    generation = live.generation
    assert client.post(path + "/stop-task", json={}, headers=headers).status_code == 200
    assert live.generation > generation
    assert client.get("/v1/voice/sessions/" + str(uuid4())).status_code == 404
    assert client.post(path + "/close", json={}, headers=headers).status_code == 200
    assert client.post(path + "/close", json={}, headers=headers).status_code == 200
    saved = client.get(path).json()
    assert saved["usage_final"] and "provider_id" not in saved
    assert socket.closed
    client.cookies.clear()
    assert client.get(path).status_code == 401


def test_public_prefix_assets_auth_stream_and_security(container):
    container.settings.public_path = "/simon"
    with TestClient(create_app(container), base_url="http://localhost:8000") as client:
        for page in ("login", "chat"):
            response = client.get("/simon/" + page)
            assert response.status_code == 200
            assert 'content="/simon"' in response.text
            assert 'src="/simon/assets/app-path.js"' in response.text
            assert 'href="/assets/' not in response.text
            assert response.headers["Vercel-CDN-Cache-Control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            assert "microphone=(self)" in response.headers["permissions-policy"]
        assert client.get("/simon/assets/voice.js").status_code == 200
        assert client.get("/auth/config").status_code == 404
        assert client.get("/simon/").url.path == "/simon/login"
        login = client.post(
            "/simon/auth/dev-login",
            headers={"Origin": "http://localhost:8000"},
            json={"token": "test-development-secret-32-characters"},
        )
        assert login.status_code == 200
        assert client.get("/simon/auth/session").status_code == 200
        assert not client.get("/simon/v1/voice").json()["enabled"]
        assert client.get("/simon/v1/home/devices").status_code == 200
        assert client.get("/simon/auth/google/callback").url.path == "/simon/chat"


def test_dashboard_control_color_and_duplicate_receipts(client, container, auth_headers):
    from simon.domain.home import HomeStatus
    from tests.unit.test_home_adapters import device

    _, actor = container.identity.resolve(client.cookies.get("simon_session"))
    light = device().model_copy(
        update={
            "household_id": actor.household_id,
            "control_enabled": True,
            "load_type": "lighting",
        }
    )
    home = container.connected.home
    home.devices = (light,)
    state = {"color": "#ffffff", "brightness": 50}
    writes = []
    home.lifx.read = lambda d: HomeStatus(
        device_id=d.id,
        on=True,
        online=True,
        color=state["color"],
        brightness=state["brightness"],
        capabilities=("power", "color", "brightness"),
    )

    def send(d, change):
        writes.append(change)
        state.update(color=change.color, brightness=change.brightness)

    home.lifx.set = send
    path = "/v1/home/devices/" + light.id + "/control"
    body = {"color": "#ff0000", "brightness": 65, "idempotency_key": str(uuid4())}
    assert client.post(path, json=body).status_code == 403
    result = client.post(path, json=body, headers=auth_headers)
    assert result.status_code == 200 and result.json()["verified"]
    assert result.json()["thread_id"] is None
    assert client.post(path, json=body, headers=auth_headers).json() == result.json()
    assert len(writes) == 1
    assert (
        client.post(path, json=body | {"brightness": 10}, headers=auth_headers).status_code == 400
    )
    assert len(writes) == 1
