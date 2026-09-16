import json

from simon.config import Settings
from simon.domain.errors import ModelError
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_model_runs import FakeModel


def frames(response):
    result = []
    for frame in response.text.strip().split("\n\n"):
        lines = frame.splitlines()
        result.append((lines[0][7:], json.loads(lines[1][6:])))
    return result


def test_streaming_api_replay_error_and_controls(client, container, auth_headers):
    model = FakeModel()
    container.conversations = ModelConversationService(
        container.store, container.audit, model, Settings()
    )
    thread = client.post(
        "/v1/threads",
        headers=auth_headers,
        json={"title": "Stream", "idempotency_key": "stream-thread-001"},
    ).json()
    base = f"/v1/threads/{thread['id']}"
    body = {
        "text": "Hello",
        "profile": "quick",
        "answer_length": "brief",
        "idempotency_key": "stream-request-001",
    }
    assert client.post(base + "/runs/stream", json=body).status_code == 403
    assert client.get(base + "/latest-run").json() is None
    response = client.post(base + "/runs/stream", headers=auth_headers, json=body)
    assert response.status_code == 200
    events = frames(response)
    assert [e[0] for e in events] == ["run.started", "text.delta", "text.delta", "run.completed"]
    run = events[-1][1]
    assert run["profile"]["selected"] == "quick"
    assert client.get(base + "/latest-run").json() == run
    replay = client.post(base + "/runs/stream", headers=auth_headers, json=body)
    assert frames(replay) == [("run.completed", run)]
    assert len(model.requests) == 1
    assert client.post(f"/v1/runs/{run['id']}/cancel", headers=auth_headers).status_code == 204

    def fail():
        raise ModelError("model_rate_limit")

    model.action = fail
    failed = client.post(
        base + "/runs/stream",
        headers=auth_headers,
        json={**body, "idempotency_key": "stream-request-002"},
    )
    assert frames(failed)[-1][1]["reason"] == "model_rate_limit"
    assert len(client.get(base + "/messages").json()) == 2


def test_stream_rechecks_access_before_delivery(client, container, auth_headers):
    model = FakeModel()
    container.conversations = ModelConversationService(
        container.store, container.audit, model, Settings()
    )
    thread = client.post(
        "/v1/threads",
        headers=auth_headers,
        json={"title": "Stream", "idempotency_key": "stream-thread-001"},
    ).json()
    from simon.domain.identity import DEV_ACTOR_ID

    model.action = lambda: container.store.revoke_sessions(DEV_ACTOR_ID)
    response = client.post(
        f"/v1/threads/{thread['id']}/runs/stream",
        headers=auth_headers,
        json={"text": "Hello", "idempotency_key": "stream-request-001"},
    )
    assert "A useful" not in response.text
    assert frames(response)[-1][0] == "run.error"
