import json
from uuid import uuid4

import httpx
import pytest

from simon.adapters.tool_preflight import integration_status, uses_network
from simon.adapters.web_research import ENDPOINT, WebResearchTransport, web_search_definition
from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext, Channel
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext, ToolExecutionError


def response():
    return {
        "status": "completed",
        "output": [
            {
                "type": "web_search_call",
                "status": "completed",
                "action": {
                    "type": "search",
                    "sources": [{"url": "https://example.com/spec", "title": "Spec"}],
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Supplier states no minimum order [1].",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/spec",
                                "title": "Spec",
                                "start_index": 0,
                                "end_index": 30,
                            }
                        ],
                    }
                ],
            },
        ],
    }


@pytest.fixture
def runtime():
    actor = ActorContext(
        actor_id=uuid4(), workspace_id=uuid4(), channel=Channel.API, scopes=frozenset({"jobs:read"})
    )
    definition = web_search_definition(
        model="configured-model",
        workspace_id=actor.workspace_id,
        actor_ids=[actor.actor_id],
        enabled=True,
    )
    context = ToolExecutionContext(
        actor_id=actor.actor_id,
        workspace_id=actor.workspace_id,
        run_id=uuid4(),
        agent_id="researcher",
        allowed_tool_ids=frozenset({definition.id}),
        scopes=actor.scopes,
    )
    return actor, definition, context


def invoke(runtime, data=None, status=200, actor_override=None):
    actor, definition, context = runtime
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json=response() if data is None else data)

    transport = WebResearchTransport(
        revalidate=lambda: actor_override or actor,
        environ={"SIMON_OPENAI_API_KEY": "secret-key"},
        transport=httpx.MockTransport(respond),
    )
    result = transport(definition, {"query": "cut and sew suppliers minimum order"}, context)
    return result, calls


def test_search_forces_real_search_and_returns_sources_without_private_context(runtime):
    result, calls = invoke(runtime)
    assert len(calls) == 1 and str(calls[0].url) == ENDPOINT
    payload = json.loads(calls[0].content)
    assert payload["tools"] == [{"type": "web_search", "search_context_size": "medium"}]
    assert payload["tool_choice"] == "required" and payload["store"] is False
    assert payload["include"] == ["web_search_call.action.sources"]
    assert payload["model"] == "configured-model"
    assert payload["input"] == "cut and sew suppliers minimum order"
    assert str(runtime[0].workspace_id) not in calls[0].content.decode()
    assert result["sources"][0]["url"] == "https://example.com/spec"
    assert result["citations"] and result["retrieved_at"] and not result["text_truncated"]


@pytest.mark.parametrize("change", ["no_call", "incomplete", "no_citations", "no_text"])
def test_uncited_or_unfinished_answers_are_never_success(runtime, change):
    data = response()
    if change == "no_call":
        data["output"].pop(0)
    elif change == "incomplete":
        data["status"] = "incomplete"
    elif change == "no_citations":
        data["output"][1]["content"][0]["annotations"] = []
    else:
        data["output"][1]["content"][0]["text"] = ""
    with pytest.raises(ToolExecutionError):
        invoke(runtime, data)


@pytest.mark.parametrize("status", [302, 400, 401, 429, 500])
def test_provider_error_does_not_leak_body_or_follow_redirect(runtime, status):
    with pytest.raises(ToolExecutionError, match=f"HTTP {status}") as error:
        invoke(runtime, {"error": "secret-key"}, status=status)
    assert "secret-key" not in str(error.value)
    assert not error.value.unknown


def test_tenant_and_revalidation_protect_search(runtime):
    actor, definition, context = runtime
    with pytest.raises(AuthorizationError):
        invoke((actor, definition, context.model_copy(update={"actor_id": uuid4()})))
    with pytest.raises(AuthorizationError):
        invoke(runtime, actor_override=actor.model_copy(update={"scopes": frozenset()}))


def test_mutated_endpoint_and_schema_cannot_expand_access(runtime):
    actor, definition, context = runtime
    with pytest.raises(ToolCatalogError):
        invoke((actor, definition.model_copy(update={"endpoint": "https://evil.test"}), context))
    calls = []
    transport = WebResearchTransport(
        revalidate=lambda: actor,
        environ={"SIMON_OPENAI_API_KEY": "secret-key"},
        transport=httpx.MockTransport(lambda request: calls.append(request)),
    )
    with pytest.raises(ToolCatalogError):
        transport(
            definition.model_copy(update={"input_schema": {"type": "object"}}),
            {"query": "hello", "endpoint": "https://evil.test"},
            context,
        )
    assert calls == []


def test_preflight_requires_operator_grant_and_network_classification(runtime):
    actor, definition, _ = runtime
    assert uses_network(definition.model_copy(update={"settings": {}}))
    assert integration_status(definition, actor, {})[0] == "unconfigured"
    assert integration_status(definition, actor, {"SIMON_OPENAI_API_KEY": "key"})[0] == "configured"
    assert (
        integration_status(
            definition,
            actor.model_copy(update={"actor_id": uuid4()}),
            {"SIMON_OPENAI_API_KEY": "key"},
        )[0]
        == "permission_required"
    )


def test_results_are_bounded_and_secrets_redacted(runtime):
    data = response()
    data["output"][1]["content"][0]["text"] = "secret-key" + "x" * 20000
    result, _ = invoke(runtime, data)
    assert result["text_truncated"] and "secret-key" not in result["text"]
    assert len(result["text"]) <= 16000


@pytest.mark.parametrize("field", ["sources", "content", "annotations"])
def test_malformed_provider_collections_are_known_read_failures(runtime, field):
    data = response()
    if field == "sources":
        data["output"][0]["action"][field] = 10
    elif field == "content":
        data["output"][1][field] = 10
    else:
        data["output"][1]["content"][0][field] = 10
    with pytest.raises(ToolExecutionError) as error:
        invoke(runtime, data)
    assert error.value.unknown is False


def test_deeply_nested_provider_json_is_a_known_read_failure(runtime):
    actor, definition, context = runtime
    transport = WebResearchTransport(
        revalidate=lambda: actor,
        environ={"SIMON_OPENAI_API_KEY": "secret-key"},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content="[" * 10000 + "0" + "]" * 10000)
        ),
    )
    with pytest.raises(ToolExecutionError, match="invalid JSON") as error:
        transport(definition, {"query": "supplier information"}, context)
    assert error.value.unknown is False


def test_legacy_browser_manifest_is_blocked_before_read_dispatch(runtime):
    from simon.adapters.browser_tools import browser_tool_definitions

    actor, _, _ = runtime
    definition = next(t for t in browser_tool_definitions(enabled=True) if t.id == "browser.read")
    definition = definition.model_copy(update={"settings": {"public_web": True}})
    assert integration_status(definition, actor, {})[0] == "configured"
    legacy = definition.model_copy(
        update={
            "output_schema": {"type": "object", "required": ["stdout", "exit_code"]},
        }
    )
    state, reasons = integration_status(legacy, actor, {})
    assert state == "unconfigured" and "current tool template" in reasons[0]
