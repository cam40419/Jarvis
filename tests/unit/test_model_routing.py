from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from simon.adapters.model_endpoints import ModelEndpointClient, ModelEndpointError
from simon.domain.model_routing import (
    ModelEndpoint,
    RoutingRequest,
    TextGenerationRequest,
)
from simon.services.model_router import ModelRouter, ModelRoutingError


def endpoint(name: str = "local", **overrides: Any) -> ModelEndpoint:
    return ModelEndpoint.model_validate(
        {
            "id": name,
            "provider": "openai_compatible",
            "model": "installed-model:custom-version",
            "base_url": "http://127.0.0.1:11434/v1",
            "local": True,
            "tier": "economy",
            **overrides,
        }
    )


def frontier(**overrides: Any) -> ModelEndpoint:
    return endpoint(
        "frontier",
        **{
            "provider": "openai_responses",
            "model": "administrator-selected-frontier-version",
            "base_url": "https://api.example.test/v1",
            "local": False,
            "api_key_env": "TEST_PROVIDER_KEY",
            "tier": "frontier",
            "reasoning_efforts": ["low", "medium", "high", "ultra"],
            "input_cost_per_million_usd": 10,
            "output_cost_per_million_usd": 30,
            **overrides,
        },
    )


def test_simple_work_prefers_local_but_critical_work_requires_frontier() -> None:
    router = ModelRouter(
        [frontier(), endpoint(), endpoint("standard", tier="standard")],
        environ={"TEST_PROVIDER_KEY": "not-a-real-key"},
    )
    simple = router.route(RoutingRequest())
    assert simple.endpoint_id == "local"
    assert simple.estimated_cost_usd == 0
    medium = router.route(RoutingRequest(depth=3))
    assert medium.endpoint_id == "standard"
    important = router.route(RoutingRequest(depth=1, importance=4))
    assert important.endpoint_id == "frontier"
    assert important.reasoning_effort == "high"
    assert "not-a-real-key" not in important.model_dump_json()
    deepest = router.route(RoutingRequest(depth=5))
    assert deepest.reasoning_effort == "ultra"


def test_missing_frontier_key_blocks_instead_of_silently_downgrading() -> None:
    router = ModelRouter([endpoint(), frontier()], environ={})
    with pytest.raises(ModelRoutingError) as caught:
        router.route(RoutingRequest(importance=4))
    assert caught.value.code == "no_eligible_model"
    assert "credentials_unavailable" in caught.value.rejections[1].reasons
    assert "below_quality_floor" in caught.value.rejections[0].reasons


@pytest.mark.parametrize(
    ("routing_request", "reason"),
    [
        (RoutingRequest(required_capabilities=frozenset({"vision"})), "missing_capabilities"),
        (RoutingRequest(input_tokens=32768), "context_window_exceeded"),
        (RoutingRequest(output_tokens=10000), "output_limit_exceeded"),
        (RoutingRequest(budget_usd=0), "estimated_budget_exceeded"),
        (RoutingRequest(privacy="local_only"), "privacy_requires_local"),
    ],
)
def test_manual_override_cannot_bypass_constraints(
    routing_request: RoutingRequest, reason: str
) -> None:
    router = ModelRouter([frontier()], environ={"TEST_PROVIDER_KEY": "synthetic"})
    with pytest.raises(ModelRoutingError) as caught:
        router.route(routing_request.model_copy(update={"model_override": "frontier"}))
    assert caught.value.code == "model_override_unavailable"
    assert reason in caught.value.rejections[0].reasons


def test_manual_override_cannot_reduce_quality_floor() -> None:
    with pytest.raises(ModelRoutingError):
        ModelRouter([endpoint()]).route(RoutingRequest(depth=4, model_override="local"))


def test_budget_requires_known_pricing_and_never_assumes_cloud_is_free() -> None:
    no_price = frontier(input_cost_per_million_usd=None, output_cost_per_million_usd=None)
    router = ModelRouter([no_price], environ={"TEST_PROVIDER_KEY": "synthetic"})
    assert router.route(RoutingRequest()).estimated_cost_usd is None
    with pytest.raises(ModelRoutingError) as caught:
        router.route(RoutingRequest(budget_usd=1))
    assert caught.value.rejections[0].reasons == ("pricing_unknown",)
    priced = ModelRouter([frontier()], environ={"TEST_PROVIDER_KEY": "synthetic"})
    assert (
        priced.route(RoutingRequest(input_tokens=1000, output_tokens=100)).estimated_cost_usd
        == 0.013
    )


def test_configured_priority_and_cost_rank_same_tier_candidates() -> None:
    expensive = endpoint("expensive", input_cost_per_million_usd=5, output_cost_per_million_usd=20)
    cheap = endpoint("cheap", input_cost_per_million_usd=1, output_cost_per_million_usd=2)
    chosen = ModelRouter([expensive, cheap]).route(RoutingRequest())
    assert chosen.endpoint_id == "cheap"
    assert chosen.alternative_endpoint_ids == ("expensive",)


@pytest.mark.parametrize(
    "url",
    [
        "http://models.example.test/v1",
        "https://user:secret@example.test/v1",
        "https://example.test/v1?api_key=secret",
        "https://example.test/v1#fragment",
        "file:///etc/secrets",
        "https://example.test:99999/v1",
    ],
)
def test_endpoint_urls_cannot_embed_secrets_or_use_unencrypted_remote_hosts(url: str) -> None:
    with pytest.raises(ValidationError):
        endpoint(base_url=url)


def test_configuration_rejects_secret_fields_bad_efforts_and_duplicate_ids() -> None:
    with pytest.raises(ValidationError):
        endpoint(api_key="raw-secret")
    with pytest.raises(ValidationError):
        endpoint(local=False)
    with pytest.raises(ValidationError):
        endpoint(provider="gemini", reasoning_efforts=("ultra",))
    with pytest.raises(ValueError, match="unique"):
        ModelRouter([endpoint(), endpoint()])


def response_for(provider: str) -> dict[str, Any]:
    if provider == "openai_responses":
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Done"}]}],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        }
    if provider == "openai_compatible":
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": "Done"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5},
        }
    if provider == "anthropic":
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Done"}],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        }
    return {
        "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "Done"}]}}],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 3,
            "thoughtsTokenCount": 2,
        },
    }


@pytest.mark.parametrize(
    ("provider", "path", "header"),
    [
        ("openai_responses", "/v1/responses", "authorization"),
        ("openai_compatible", "/v1/chat/completions", "authorization"),
        ("anthropic", "/v1/messages", "x-api-key"),
        ("gemini", "/v1/models/selected-model:generateContent", "x-goog-api-key"),
    ],
)
def test_provider_transports_use_expected_wire_contract(
    provider: str, path: str, header: str
) -> None:
    configured = frontier(provider=provider, model="selected-model", reasoning_efforts=("high",))
    environment = {"TEST_PROVIDER_KEY": "synthetic-secret"}
    decision = ModelRouter([configured], environ=environment).route(RoutingRequest(depth=4))
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == path
        assert "synthetic-secret" in request.headers[header]
        assert "synthetic-secret" not in str(request.url)
        payload = json.loads(request.content)
        if provider == "openai_responses":
            assert "text" not in payload
            assert "tools" not in payload
            assert "tool_choice" not in payload
            assert "parallel_tool_calls" not in payload
            assert payload["reasoning"] == {"effort": "high"}
            assert payload["store"] is False
            assert payload["max_output_tokens"] == 512
        elif provider == "openai_compatible":
            assert payload["reasoning_effort"] == "high"
            assert payload["messages"][0] == {"role": "system", "content": "Be precise"}
        elif provider == "anthropic":
            assert payload["output_config"] == {"effort": "high"}
            assert request.headers["anthropic-version"] == "2023-06-01"
        else:
            assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "HIGH"}
            assert payload["systemInstruction"] == {"parts": [{"text": "Be precise"}]}
        return httpx.Response(200, json=response_for(provider))

    client = ModelEndpointClient(
        [configured], environ=environment, transport=httpx.MockTransport(handle)
    )
    result = client.generate(
        decision,
        TextGenerationRequest(
            prompt="Return an answer", system="Be precise", max_output_tokens=512
        ),
    )
    assert result.text == "Done"
    assert result.input_tokens == 12
    assert result.output_tokens == 5
    assert len(seen) == 1


def test_responses_transport_transmits_explicit_strict_response_schema() -> None:
    configured = frontier()
    environment = {"TEST_PROVIDER_KEY": "synthetic-secret"}
    decision = ModelRouter([configured], environ=environment).route(RoutingRequest())
    schema = {
        "type": "object",
        "properties": {"type": {"type": "string", "enum": ["final"]}, "output": {"type": "string"}},
        "required": ["type", "output"],
        "additionalProperties": False,
    }
    output = '{"type":"final","output":"Completed"}'
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert payload["text"] == {
            "format": {
                "type": "json_schema",
                "name": "simon_controller",
                "strict": True,
                "schema": schema,
            }
        }
        assert payload["input"] == "Return a final result"
        assert payload["store"] is False
        response = response_for("openai_responses")
        response["output"][0]["content"][0]["text"] = output
        return httpx.Response(200, json=response)

    client = ModelEndpointClient(
        [configured], environ=environment, transport=httpx.MockTransport(handle)
    )
    result = client.generate(
        decision, TextGenerationRequest(prompt="Return a final result", response_schema=schema)
    )
    assert result.text == output
    assert len(seen) == 1


def test_controller_mode_requires_a_schema() -> None:
    with pytest.raises(ValidationError, match="requires a response schema"):
        TextGenerationRequest(prompt="Return the next action", controller_mode=True)
    assert TextGenerationRequest(prompt="Plain generation").controller_mode is False


def test_responses_controller_forces_one_function_and_ignores_accompanying_text() -> None:
    configured = frontier(capabilities=frozenset({"text", "tools"}))
    environment = {"TEST_PROVIDER_KEY": "synthetic-secret"}
    decision = ModelRouter([configured], environ=environment).route(
        RoutingRequest(required_capabilities=frozenset({"text", "tools"}))
    )
    schema = {
        "type": "object",
        "properties": {"output": {"type": "string"}},
        "required": ["output"],
        "additionalProperties": False,
    }
    arguments = '{"output":"This is the selected controller result"}'
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = json.loads(request.content)
        assert payload["tools"] == [
            {
                "type": "function",
                "name": "simon_controller",
                "description": "Return one next controller action or final result",
                "strict": True,
                "parameters": schema,
            }
        ]
        assert payload["tool_choice"] == {"type": "function", "name": "simon_controller"}
        assert payload["parallel_tool_calls"] is False
        assert "text" not in payload
        assert payload["store"] is False
        response = response_for("openai_responses")
        response["output"] = [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"type":"tool"}'}],
            },
            {"type": "function_call", "name": "simon_controller", "arguments": arguments},
            {
                "type": "message",
                "content": [{"type": "output_text", "text": '{"type":"final"}'}],
            },
        ]
        return httpx.Response(200, json=response)

    client = ModelEndpointClient(
        [configured], environ=environment, transport=httpx.MockTransport(handle)
    )
    result = client.generate(
        decision,
        TextGenerationRequest(
            prompt="Return the next action", response_schema=schema, controller_mode=True
        ),
    )
    assert result.text == arguments
    assert result.input_tokens == 12
    assert result.output_tokens == 5
    assert len(seen) == 1


@pytest.mark.parametrize(
    "failure",
    ["no_call", "multiple_calls", "wrong_name", "wrong_type", "wrong_arguments", "empty_arguments"],
)
def test_controller_mode_rejects_ambiguous_or_unexpected_output(failure: str) -> None:
    configured = frontier(capabilities=frozenset({"text", "tools"}))
    environment = {"TEST_PROVIDER_KEY": "synthetic-secret"}
    decision = ModelRouter([configured], environ=environment).route(
        RoutingRequest(required_capabilities=frozenset({"text", "tools"}))
    )
    call: dict[str, Any] = {
        "type": "function_call",
        "name": "simon_controller",
        "arguments": '{"output":"synthetic-sensitive-provider-data"}',
    }
    output = [call]
    if failure == "no_call":
        output = response_for("openai_responses")["output"]
    elif failure == "multiple_calls":
        output.append(dict(call))
    elif failure == "wrong_name":
        call["name"] = "other_function"
    elif failure == "wrong_type":
        call["type"] = "web_search_call"
    elif failure == "wrong_arguments":
        call["arguments"] = {"output": "synthetic-sensitive-provider-data"}
    else:
        call["arguments"] = ""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "completed", "output": output})

    client = ModelEndpointClient(
        [configured], environ=environment, transport=httpx.MockTransport(handle)
    )
    with pytest.raises(ModelEndpointError) as caught:
        client.generate(
            decision,
            TextGenerationRequest(
                prompt="Return the next action", response_schema={}, controller_mode=True
            ),
        )
    assert caught.value.code == "invalid_model_response"
    assert caught.value.may_have_been_dispatched is True
    assert "synthetic-sensitive" not in str(caught.value)
    assert len(seen) == 1


@pytest.mark.parametrize("provider", ["openai_compatible", "anthropic", "gemini"])
def test_controller_mode_rejects_other_transports_before_dispatch(provider: str) -> None:
    configured = frontier(
        provider=provider, reasoning_efforts=(), capabilities=frozenset({"text", "tools"})
    )
    environment = {"TEST_PROVIDER_KEY": "synthetic-secret"}
    decision = ModelRouter([configured], environ=environment).route(
        RoutingRequest(required_capabilities=frozenset({"text", "tools"}))
    )
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response_for(provider))

    client = ModelEndpointClient(
        [configured], environ=environment, transport=httpx.MockTransport(handle)
    )
    with pytest.raises(ModelEndpointError) as caught:
        client.generate(
            decision,
            TextGenerationRequest(
                prompt="Return the next action", response_schema={}, controller_mode=True
            ),
        )
    assert caught.value.code == "unsupported_generation_mode"
    assert caught.value.may_have_been_dispatched is False
    assert seen == []


def test_controller_mode_requires_declared_tools_capability_before_dispatch() -> None:
    configured = frontier()
    environment = {"TEST_PROVIDER_KEY": "synthetic-secret"}
    decision = ModelRouter([configured], environ=environment).route(RoutingRequest())
    client = ModelEndpointClient([configured], environ=environment)
    with pytest.raises(ModelEndpointError) as caught:
        client.generate(
            decision,
            TextGenerationRequest(
                prompt="Return the next action", response_schema={}, controller_mode=True
            ),
        )
    assert caught.value.code == "unsupported_generation_mode"
    assert caught.value.may_have_been_dispatched is False


@pytest.mark.parametrize("provider", ["openai_compatible", "anthropic", "gemini"])
@pytest.mark.parametrize(
    "schema", [{}, {"type": "object", "properties": {}}], ids=["empty", "object"]
)
def test_unsupported_transports_reject_response_schema_before_any_dispatch(
    provider, schema
) -> None:
    configured = frontier(provider=provider, reasoning_efforts=())
    environment = {"TEST_PROVIDER_KEY": "synthetic-secret"}
    decision = ModelRouter([configured], environ=environment).route(RoutingRequest())
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=response_for(provider))

    client = ModelEndpointClient(
        [configured], environ=environment, transport=httpx.MockTransport(handle)
    )
    with pytest.raises(ModelEndpointError) as caught:
        client.generate(
            decision, TextGenerationRequest(prompt="Return a result", response_schema=schema)
        )
    assert caught.value.code == "unsupported_generation_mode"
    assert caught.value.may_have_been_dispatched is False
    assert seen == []


@pytest.mark.parametrize("failure", ["timeout", "server", "redirect", "invalid", "oversize"])
def test_provider_errors_are_redacted_and_never_retried(failure: str) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-sensitive-provider-data")
        if failure == "server":
            return httpx.Response(500, text="synthetic-sensitive-provider-data")
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://elsewhere.example.test"})
        if failure == "invalid":
            return httpx.Response(200, text="synthetic-sensitive-provider-data")
        return httpx.Response(200, content=b"x" * 101)

    configured = endpoint()
    decision = ModelRouter([configured]).route(RoutingRequest())
    client = ModelEndpointClient(
        [configured], transport=httpx.MockTransport(handle), max_response_bytes=100
    )
    with pytest.raises(ModelEndpointError) as caught:
        client.generate(decision, TextGenerationRequest(prompt="Hello"))
    assert caught.value.may_have_been_dispatched
    assert "synthetic-sensitive" not in str(caught.value)
    assert len(seen) == 1


def test_credential_revocation_is_rechecked_at_dispatch() -> None:
    environment = {"TEST_PROVIDER_KEY": "synthetic"}
    configured = frontier()
    decision = ModelRouter([configured], environ=environment).route(RoutingRequest())
    environment.clear()
    client = ModelEndpointClient([configured], environ=environment)
    with pytest.raises(ModelEndpointError) as caught:
        client.generate(decision, TextGenerationRequest(prompt="Hello"))
    assert caught.value.code == "route_unavailable"
    assert not caught.value.may_have_been_dispatched


@pytest.mark.parametrize("capability", ["tools", "vision"])
def test_text_adapter_rejects_tool_or_media_work_before_network_access(capability: str) -> None:
    configured = endpoint(capabilities=frozenset({"text", "vision", "tools"}))
    decision = ModelRouter([configured]).route(
        RoutingRequest(required_capabilities=frozenset({capability}))
    )
    with pytest.raises(ModelEndpointError) as caught:
        ModelEndpointClient([configured]).generate(decision, TextGenerationRequest(prompt="Hello"))
    assert caught.value.code == "unsupported_generation_mode"


def test_generation_cannot_exceed_routed_output_reservation() -> None:
    configured = endpoint()
    decision = ModelRouter([configured]).route(RoutingRequest(output_tokens=10))
    with pytest.raises(ModelEndpointError) as caught:
        ModelEndpointClient([configured]).generate(decision, TextGenerationRequest(prompt="Hello"))
    assert caught.value.code == "output_reservation_exceeded"


def test_truncated_output_is_reported_not_claimed_complete() -> None:
    configured = endpoint()
    decision = ModelRouter([configured]).route(RoutingRequest())
    client = ModelEndpointClient(
        [configured],
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"finish_reason": "length", "message": {"content": "Partial"}}]},
            )
        ),
    )
    assert client.generate(decision, TextGenerationRequest(prompt="Hello")).truncated
