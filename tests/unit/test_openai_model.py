import json

import httpx
import pytest
from openai import OpenAI

from jarvis.adapters.openai_model import OpenAIModel
from jarvis.domain.errors import ModelError
from jarvis.domain.model import ModelRequest


def adapter(monkeypatch, responder):
    def factory(**kwargs):
        assert kwargs["base_url"] == "https://api.openai.com/v1"
        assert kwargs["max_retries"] == 0 and kwargs["timeout"] == 30
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(responder)))

    monkeypatch.setattr("jarvis.adapters.openai_model.OpenAI", factory)
    return OpenAIModel("test-key")


def request():
    return ModelRequest(model="gpt-5.4-mini", instructions="Fixed instructions", input_text="Hello")


def output(status="completed", text="Hello from the model"):
    return {
        "id": "resp_test",
        "model": "gpt-5.4-mini",
        "status": status,
        "output": [
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "usage": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
    }


def test_real_sdk_request_shape_and_token_gate(monkeypatch):
    requests = []

    def respond(req):
        payload = json.loads(req.content)
        requests.append(payload)
        if req.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 20, "object": "response.input_tokens"})
        assert payload["store"] is False and payload["tools"] == []
        assert payload["truncation"] == "disabled" and payload["max_output_tokens"] == 2048
        return httpx.Response(200, json=output())

    answer = adapter(monkeypatch, respond).generate(request())
    assert answer.text == "Hello from the model"
    assert len(requests) == 2
    assert requests[0]["input"] == requests[1]["input"] == "Hello"
    assert requests[0]["instructions"] == requests[1]["instructions"]


def test_over_budget_never_generates(monkeypatch):
    def respond(req):
        assert req.url.path.endswith("input_tokens")
        return httpx.Response(200, json={"input_tokens": 20001})

    with pytest.raises(ModelError) as error:
        adapter(monkeypatch, respond).generate(request())
    assert error.value.reason == "model_input_limit"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "model_credentials"),
        (403, "model_credentials"),
        (429, "model_rate_limit"),
        (500, "model_unavailable"),
    ],
)
def test_provider_errors_are_sanitized(monkeypatch, status, reason):
    def respond(req):
        return httpx.Response(status, json={"error": {"message": "sensitive-provider-detail"}})

    with pytest.raises(ModelError) as error:
        adapter(monkeypatch, respond).generate(request())
    assert error.value.reason == reason
    assert "sensitive-provider-detail" not in str(error.value)


@pytest.mark.parametrize("timeout", [True, False])
def test_transport_failures(monkeypatch, timeout):
    def respond(req):
        if timeout:
            raise httpx.ReadTimeout("sensitive detail")
        raise httpx.ConnectError("sensitive detail")

    with pytest.raises(ModelError) as error:
        adapter(monkeypatch, respond).generate(request())
    assert error.value.reason == ("model_timeout" if timeout else "model_unavailable")


@pytest.mark.parametrize(
    "result", [output("incomplete"), output(text=""), {**output(), "usage": None}]
)
def test_incomplete_output_not_published(monkeypatch, result):
    def respond(req):
        return httpx.Response(
            200, json={"input_tokens": 1} if req.url.path.endswith("input_tokens") else result
        )

    with pytest.raises(ModelError) as error:
        adapter(monkeypatch, respond).generate(request())
    assert error.value.reason == "model_incomplete"


def test_model_configuration_requires_key_and_selects_adapter():
    from pydantic import SecretStr, ValidationError

    from jarvis.api.app import AppContainer
    from jarvis.config import Settings
    from jarvis.services.model_conversations import ModelConversationService

    with pytest.raises(ValidationError, match="JARVIS_OPENAI_API_KEY"):
        Settings(_env_file=None, model_provider="openai", openai_api_key=None)
    with pytest.raises(ValidationError, match="JARVIS_OPENAI_API_KEY"):
        Settings(model_provider="openai", openai_api_key=SecretStr("your-key-placeholder"))
    container = AppContainer(
        settings=Settings(model_provider="openai", openai_api_key=SecretStr("test-key"))
    )
    assert isinstance(container.conversations, ModelConversationService)
