from fastapi.testclient import TestClient

from simon.api.app import create_app
from simon.services.work_sessions import WorkSessionService
from tests.contract.test_work_sessions import setup_sessions


def test_background_api_acknowledges_before_execution_and_reconnects(container):
    sessions, _, model, thread, _, token = setup_sessions(container.store)
    container.work_sessions = sessions
    with TestClient(create_app(container), base_url="http://localhost:8000") as client:
        client.cookies.set("simon_session", token)
        headers = {
            "Origin": "http://localhost:8000",
            "X-CSRF-Token": client.get("/auth/session").json()["csrf_token"],
        }
        body = {"text": "Keep working", "idempotency_key": "api-background"}
        path = f"/v1/work-sessions/threads/{thread.id}"
        assert client.post(path, json=body).status_code == 403
        result = client.post(path, json=body, headers=headers)
        assert result.status_code == 202 and result.json()["status"] == "queued"
        assert not model.requests
        identifier = result.json()["id"]
    # Browser is closed; a fresh service instance consumes the saved queue.
    WorkSessionService(sessions.store, sessions.identity, sessions.conversations).tick()
    with TestClient(create_app(container), base_url="http://localhost:8000") as client:
        client.cookies.set("simon_session", token)
        assert client.get(f"/v1/work-sessions/{identifier}").json()["status"] == "succeeded"
