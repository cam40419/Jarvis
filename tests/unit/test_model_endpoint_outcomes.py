"""Known provider terminal outcomes retain usage without returning private bodies."""

import json

import httpx
import pytest
from pydantic import ValidationError

from simon.adapters.model_endpoints import ModelEndpointClient, ModelEndpointError
from simon.domain.model_routing import RoutingRequest, TextGenerationRequest, TextGenerationResult
from simon.services.agent_worker import _controller_schema
from simon.services.model_router import ModelRouter
from tests.unit.test_model_routing import frontier


def generate_document(document, *, controller):
    configured = frontier(capabilities=frozenset({"text", "tools"}))
    environment = {"TEST_PROVIDER_KEY": "test-only-key"}
    decision = ModelRouter((configured,), environ=environment).route(
        RoutingRequest(
            required_capabilities=frozenset({"text", "tools"} if controller else {"text"})
        )
    )
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=document)

    client = ModelEndpointClient(
        (configured,), environ=environment, transport=httpx.MockTransport(handle)
    )
    request = TextGenerationRequest(
        prompt="Return the final answer.",
        controller_mode=controller,
        response_schema=_controller_schema(()) if controller else None,
    )
    return client, decision, request, requests


def usage():
    return {
        "input_tokens": 12000,
        "output_tokens": 8192,
        "output_tokens_details": {"reasoning_tokens": 8192},
    }


@pytest.mark.parametrize("controller", [False, True])
@pytest.mark.parametrize("reasoning_only", [False, True])
def test_empty_output_limit_result_preserves_usage_and_does_not_retry(controller, reasoning_only):
    document = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "reasoning", "summary": []}] if reasoning_only else [],
        "usage": usage(),
    }
    client, decision, request, seen = generate_document(document, controller=controller)
    result = client.generate(decision, request)
    assert result.text == ""
    assert result.truncated and not result.refused
    assert result.response_reason == "max_output_tokens"
    assert (result.input_tokens, result.output_tokens, result.reasoning_tokens) == (
        12000,
        8192,
        8192,
    )
    assert len(seen) == 1
    if controller:
        payload = json.loads(seen[0].content)
        branches = payload["tools"][0]["parameters"]["properties"]["action"]["anyOf"]
        assert [branch["properties"]["type"]["enum"] for branch in branches] == [["final"]]


@pytest.mark.parametrize("controller", [False, True])
@pytest.mark.parametrize("partial", ["", '{"action":{"type":"final","output":"Partial'])
def test_partial_output_limit_stays_truncated_and_never_becomes_a_deliverable(controller, partial):
    item = (
        {"type": "function_call", "name": "simon_controller", "arguments": partial}
        if controller
        else {"type": "message", "content": [{"type": "output_text", "text": partial}]}
    )
    client, decision, request, seen = generate_document(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [item],
            "usage": usage(),
        },
        controller=controller,
    )
    result = client.generate(decision, request)
    assert result.truncated and result.text == partial
    assert result.output_tokens == 8192 and len(seen) == 1


@pytest.mark.parametrize("controller", [False, True])
@pytest.mark.parametrize("accompanying_content", [False, True])
def test_explicit_refusal_discards_all_bodies_and_preserves_usage(controller, accompanying_content):
    output = [
        {"type": "reasoning", "summary": []},
        {
            "type": "message",
            "content": [{"type": "refusal", "refusal": "PRIVATE refusal explanation"}],
        },
    ]
    if accompanying_content:
        output.append(
            {"type": "function_call", "name": "simon_controller", "arguments": "PRIVATE args"}
            if controller
            else {"type": "message", "content": [{"type": "output_text", "text": "PRIVATE text"}]}
        )
    client, decision, request, seen = generate_document(
        {"status": "completed", "output": output, "usage": usage()}, controller=controller
    )
    result = client.generate(decision, request)
    assert result.refused and not result.truncated
    assert result.response_reason == "refusal" and result.text == ""
    assert (result.input_tokens, result.output_tokens, result.reasoning_tokens) == (
        12000,
        8192,
        8192,
    )
    assert "PRIVATE" not in result.model_dump_json()
    assert len(seen) == 1


@pytest.mark.parametrize("controller", [False, True])
@pytest.mark.parametrize(
    "problem", ["completed_empty", "completed_reasoning", "other_reason", "bad_refusal"]
)
def test_unclassified_response_is_not_guessed_to_be_a_known_limit_or_refusal(controller, problem):
    document = {"status": "completed", "output": [], "usage": usage()}
    if problem == "completed_reasoning":
        document["output"] = [{"type": "reasoning", "summary": []}]
    elif problem == "other_reason":
        document.update(status="incomplete", incomplete_details={"reason": "PRIVATE unexpected"})
    elif problem == "bad_refusal":
        document["output"] = [
            {"type": "message", "content": [{"type": "refusal", "refusal": {"PRIVATE": True}}]}
        ]
    client, decision, request, seen = generate_document(document, controller=controller)
    with pytest.raises(ModelEndpointError) as rejected:
        client.generate(decision, request)
    assert rejected.value.code == "invalid_model_response"
    assert rejected.value.may_have_been_dispatched
    assert "PRIVATE" not in str(rejected.value) and len(seen) == 1


@pytest.mark.parametrize("problem", ["multiple_calls", "wrong_name", "hosted_tool"])
def test_incomplete_marker_does_not_accept_ambiguous_or_unexpected_controller_calls(problem):
    call = {"type": "function_call", "name": "simon_controller", "arguments": "partial"}
    output = [call]
    if problem == "multiple_calls":
        output.append(dict(call))
    elif problem == "wrong_name":
        call["name"] = "another_tool"
    else:
        call["type"] = "web_search_call"
    client, decision, request, seen = generate_document(
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": output,
        },
        controller=True,
    )
    with pytest.raises(ModelEndpointError) as rejected:
        client.generate(decision, request)
    assert rejected.value.code == "invalid_model_response" and len(seen) == 1


def test_result_requires_explicit_typed_outcome_for_empty_text_and_redacts_refusal():
    base = {"endpoint_id": "test", "model": "test", "text": ""}
    with pytest.raises(ValidationError, match="Empty output"):
        TextGenerationResult(**base)
    assert TextGenerationResult(**base, truncated=True).truncated
    assert TextGenerationResult(**base, refused=True).refused
    with pytest.raises(ValidationError, match="Refusal bodies"):
        TextGenerationResult(**{**base, "text": "PRIVATE"}, refused=True)
    with pytest.raises(ValidationError, match="refused outcome"):
        TextGenerationResult(**base, truncated=True, response_reason="refusal")
    with pytest.raises(ValidationError, match="truncated outcome"):
        TextGenerationResult(**base, refused=True, response_reason="max_output_tokens")
