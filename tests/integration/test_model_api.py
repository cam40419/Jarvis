from jarvis.config import Settings
from jarvis.domain.errors import ModelError
from jarvis.services.model_conversations import ModelConversationService
from tests.contract.test_model_runs import FakeModel


def test_model_api_and_sanitized_failure(client, auth_headers, container):
    assert client.get("/v1/assistant").json()["provider"] == "local"
    model = FakeModel()
    container.conversations = ModelConversationService(
        container.store, container.audit, model, Settings()
    )
    thread = client.post(
        "/v1/threads",
        headers=auth_headers,
        json={"title": "Real assistant path", "idempotency_key": "assistant-api-thread"},
    ).json()
    path = f"/v1/threads/{thread['id']}/runs"
    body = {"text": "A question", "idempotency_key": "assistant-api-run"}
    result = client.post(path, headers=auth_headers, json=body)
    assert result.status_code == 201
    assert client.post(path, headers=auth_headers, json=body).json() == result.json()

    def fail():
        raise ModelError("model_rate_limit")

    model.action = fail
    result = client.post(path, headers=auth_headers, json={**body, "idempotency_key": "failed-run"})
    assert result.status_code == 503
    assert result.json()["error"]["reason"] == "model_rate_limit"
