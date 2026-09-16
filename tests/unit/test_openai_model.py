import json

import httpx
import pytest
from openai import OpenAI

from simon.adapters.openai_model import OpenAIModel
from simon.domain.errors import ModelError
from simon.domain.model import ModelRequest


def adapter(monkeypatch, responder):
    def factory(**kwargs):
        assert kwargs["base_url"] == "https://api.openai.com/v1"
        assert kwargs["max_retries"] == 0 and kwargs["timeout"] == 30
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(responder)))

    monkeypatch.setattr("simon.adapters.openai_model.OpenAI", factory)
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


@pytest.mark.parametrize("never_finishes", [False, True])
def test_device_setup_has_enough_tool_rounds_but_remains_bounded(monkeypatch, never_finishes):
    names = (
        "home_list_devices",
        "home_rename_device",
        "home_setup_outlet",
        "home_organize_devices",
        "home_control",
    )
    generated, executed = [], []

    def respond(req):
        body = json.loads(req.content)
        if req.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 20})
        step = len(generated)
        generated.append(body)
        if step:
            assert body["input"][-1]["type"] == "function_call_output"
        result = output()
        if step < len(names) or never_finishes:
            result["output"] = [
                {
                    "type": "function_call",
                    "id": f"fc_{step}",
                    "call_id": f"call_{step}",
                    "name": names[step % len(names)],
                    "arguments": "{}",
                    "status": "completed",
                }
            ]
        return httpx.Response(200, json=result)

    def execute(name, args):
        executed.append(name)
        return '{"ok":true}'

    model = adapter(monkeypatch, respond)
    selected = request().model_copy(update={"tools": names})
    if never_finishes:
        with pytest.raises(ModelError):
            model.generate_with_tools(selected, None, execute)
    else:
        assert model.generate_with_tools(selected, None, execute).text == "Hello from the model"
    assert executed == list(names) and len(generated) == 6


def test_over_budget_never_generates(monkeypatch):
    def respond(req):
        assert req.url.path.endswith("input_tokens")
        return httpx.Response(200, json={"input_tokens": 20001})

    with pytest.raises(ModelError) as error:
        adapter(monkeypatch, respond).generate(request())
    assert error.value.reason == "model_input_limit"


def test_web_search_real_sdk_shape_and_citations(monkeypatch):
    from simon.adapters.model_tools import definitions

    calls = []

    def respond(req):
        body = json.loads(req.content)
        calls.append(body)
        assert body["tools"] == [{"type": "web_search"}]
        if req.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 20})
        result = output(text="Found it. [source]")
        result["output"][0]["content"][0]["annotations"] = [
            {
                "type": "url_citation",
                "title": "Product page",
                "url": "https://example.com/product",
                "start_index": 10,
                "end_index": 18,
            }
        ]
        result["output"].insert(
            0,
            {
                "type": "web_search_call",
                "id": "ws_1",
                "status": "completed",
                "action": {"type": "search", "query": "product"},
            },
        )
        return httpx.Response(200, json=result)

    answer = adapter(monkeypatch, respond).generate(
        request().model_copy(update={"tools": ("web_search",)})
    )
    assert "[1](https://example.com/product)" in answer.text
    assert answer.web_sources[0].title == "Product page" and answer.tool_calls == (
        "web_search_call",
    )
    assert len(calls) == 2
    schemas = definitions(("calendar_list_events", "propose_calendar_event", "propose_email"))
    assert all(s["strict"] and s["parameters"]["additionalProperties"] is False for s in schemas)


@pytest.mark.parametrize("mode", ["success", "unauthorized", "limit"])
def test_function_loop_carries_outputs_and_never_exposes_write_tools(monkeypatch, mode):
    calls, executed = [], []

    def respond(req):
        body = json.loads(req.content)
        if req.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 20})
        calls.append(body)
        assert body["store"] is False and body["include"] == ["reasoning.encrypted_content"]
        assert body["tools"][0]["name"] == "propose_email"
        if len(calls) == 1 or mode == "limit":
            result = output()
            result["output"] = [
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "send_email" if mode == "unauthorized" else "propose_email",
                    "arguments": '{"to":"test@example.com"}',
                }
            ]
            return httpx.Response(200, json=result)
        assert body["input"][-1]["type"] == "function_call_output"
        assert body["input"][-1]["output"] == '{"executed":false}'
        assert body["max_output_tokens"] == 2040
        return httpx.Response(200, json=output(text="Review the card."))

    model = adapter(monkeypatch, respond)
    req = request().model_copy(update={"tools": ("propose_email",)})

    def execute(name, args):
        executed.append((name, args))
        return '{"executed":false}'

    if mode == "success":
        result = model.generate_with_tools(req, None, execute)
        assert result.input_tokens == 40 and result.output_tokens == 16
        assert result.tool_calls == ("propose_email",) and len(executed) == 1
    else:
        with pytest.raises(ModelError):
            model.generate_with_tools(req, None, execute)
        assert len(calls) == (1 if mode == "unauthorized" else 4)
        assert len(executed) == (0 if mode == "unauthorized" else 3)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "model_credentials"),
        (403, "model_permissions"),
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

    from simon.api.app import AppContainer
    from simon.config import Settings
    from simon.services.model_conversations import ModelConversationService

    with pytest.raises(ValidationError, match="SIMON_OPENAI_API_KEY"):
        Settings(_env_file=None, model_provider="openai", openai_api_key=None)
    with pytest.raises(ValidationError, match="SIMON_OPENAI_API_KEY"):
        Settings(model_provider="openai", openai_api_key=SecretStr("your-key-placeholder"))
    container = AppContainer(
        settings=Settings(model_provider="openai", openai_api_key=SecretStr("test-key"))
    )
    assert isinstance(container.conversations, ModelConversationService)


@pytest.mark.parametrize("ending", ["completed", "incomplete", "missing", "cancelled"])
def test_real_sdk_incremental_stream_and_cleanup(monkeypatch, ending):
    seen = []
    closed = []

    def frame(event):
        return ("data: " + json.dumps(event) + "\n\n").encode()

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield frame({"type": "response.output_text.delta", "delta": "Hello"})
            assert seen == ["Hello"]  # Delivered before the provider finishes.
            if ending == "missing":
                return
            yield frame({"type": "response." + ending, "response": output(ending)})

        def close(self):
            closed.append(True)

    def respond(req):
        payload = json.loads(req.content)
        assert payload["reasoning"] == {"effort": "high"}
        assert payload["text"] == {"verbosity": "low"}
        if req.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 20})
        assert payload["stream"] and payload["store"] is False
        return httpx.Response(200, stream=Stream(), headers={"Content-Type": "text/event-stream"})

    def delta(text):
        if text:
            seen.append(text)
            if ending == "cancelled":
                raise ModelError("model_cancelled")

    model = adapter(monkeypatch, respond)
    selected = request().model_copy(update={"reasoning_effort": "high", "verbosity": "low"})
    if ending == "completed":
        answer = model.generate_stream(selected, delta)
        assert answer.first_text_ms is not None and answer.total_ms >= answer.first_text_ms
        assert answer.text == "Hello from the model"
    else:
        with pytest.raises(ModelError) as error:
            model.generate_stream(selected, delta)
        assert error.value.reason == (
            "model_cancelled" if ending == "cancelled" else "model_incomplete"
        )
    assert closed
