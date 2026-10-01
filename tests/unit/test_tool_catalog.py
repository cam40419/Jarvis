import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from simon.adapters.tool_transports import HttpJsonTransport, MCPTransport, TransportRegistry
from simon.domain.errors import AuthorizationError
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)
from simon.services.tool_catalog import ToolCatalog, builtin_tool_templates


def definition(**overrides):
    values = {
        "id": "acme.robotics/arm.inspect",
        "description": "Inspect an arbitrary application added after Simon was installed",
        "transport": "custom_adapter",
        "categories": frozenset({"robotics"}),
        "capabilities": frozenset({"robotics.inspect"}),
        "required_scopes": frozenset({"robotics:read"}),
        "configured": True,
        "input_schema": {
            "type": "object",
            "properties": {"part": {"type": "string"}},
            "required": ["part"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
    }
    values.update(overrides)
    return ToolDefinition(**values)


def execution_context(tool, **overrides):
    values = {
        "actor_id": uuid4(),
        "household_id": uuid4(),
        "run_id": uuid4(),
        "agent_id": "inspector-1",
        "allowed_tool_ids": frozenset({tool.id}),
        "scopes": tool.required_scopes,
    }
    values.update(overrides)
    return ToolExecutionContext(**values)


def test_extension_is_discoverable_only_with_permission_and_bound_transport():
    tool = definition()
    catalog = ToolCatalog([tool], available_transports={"custom_adapter"})
    assert catalog.discover(query="arbitrary") == ()
    assert catalog.discover(scopes=tool.required_scopes, categories={"robotics"}) == (tool,)
    assert catalog.discover(scopes=tool.required_scopes, capabilities={"robotics.inspect"}) == (
        tool,
    )
    assert ToolCatalog([tool], available_transports=()).discover(scopes=tool.required_scopes) == ()
    assert catalog.discover(scopes=tool.required_scopes, capabilities={"image.generate"}) == ()
    assert catalog.resolve(
        [tool.id], scopes=tool.required_scopes, capabilities={"robotics.inspect"}
    ) == (tool,)


@pytest.mark.parametrize("change", [{"configured": False}, {"enabled": False}])
def test_disabled_or_unconfigured_integrations_never_claim_to_be_available(change):
    tool = definition(**change)
    catalog = ToolCatalog([tool])
    assert catalog.discover(scopes=tool.required_scopes) == ()
    with pytest.raises(ToolCatalogError, match="disabled or unconfigured"):
        catalog.resolve([tool.id], scopes=tool.required_scopes)


def test_resolution_rejects_unknown_missing_scope_capability_environment_or_executor():
    tool = definition(environment_capabilities={"robotics.application"})
    catalog = ToolCatalog([tool], available_transports={tool.transport})
    with pytest.raises(ToolCatalogError, match="Unknown"):
        catalog.resolve(["missing"])
    with pytest.raises(AuthorizationError):
        catalog.resolve([tool.id])
    with pytest.raises(ToolCatalogError, match="environment"):
        catalog.resolve([tool.id], scopes=tool.required_scopes)
    with pytest.raises(ToolCatalogError, match="capabilities"):
        catalog.resolve(
            [tool.id],
            scopes=tool.required_scopes,
            environment_capabilities={"robotics.application"},
            capabilities={"cad.edit"},
        )
    with pytest.raises(ToolCatalogError, match="no execution handler"):
        ToolCatalog([tool], available_transports=()).resolve([tool.id], scopes=tool.required_scopes)


def test_catalog_owns_schemas_and_returns_independent_copies():
    tool = definition()
    catalog = ToolCatalog([tool])
    tool.input_schema.clear()
    result = catalog.resolve([tool.id], scopes=tool.required_scopes)[0]
    assert result.input_schema["required"] == ["part"]
    result.input_schema.clear()
    assert catalog.resolve([tool.id], scopes=tool.required_scopes)[0].input_schema["required"]
    with pytest.raises(ToolCatalogError, match="Duplicate"):
        ToolCatalog([tool, tool])


@pytest.mark.parametrize(
    "invalid",
    [
        {"input_schema": {"type": "nonsense"}},
        {"input_schema": {"$ref": "https://example.com/remote.json"}},
        {"settings": {"nested": {"api_key": "secret"}}},
        {"endpoint": "https://user:password@example.com/action"},
        {"endpoint": "http://example.com/action"},
        {"endpoint": "https://example.com/action?token=secret"},
        {"side_effect": True, "action_policy": "read"},
        {"transport": "http", "configured": True},
    ],
)
def test_invalid_schemas_credentials_and_endpoint_configuration_fail_early(invalid):
    with pytest.raises(ValidationError):
        definition(**invalid)


def test_templates_cover_media_engineering_and_business_but_grant_nothing():
    templates = builtin_tool_templates()
    categories = {category for item in templates for category in item.categories}
    assert {
        "image",
        "video",
        "cad",
        "pcb",
        "3d",
        "render",
        "writing",
        "files",
        "storage",
        "web",
        "data",
        "home",
        "computer",
    } <= categories
    assert all(
        not item.enabled and not item.configured and item.required_scopes for item in templates
    )
    assert ToolCatalog(templates).discover() == ()


def test_dispatch_checks_grants_schema_and_handler_then_preserves_provenance():
    tool = definition()
    context = execution_context(tool)
    registry = TransportRegistry()
    with pytest.raises(ToolCatalogError, match="No execution handler"):
        registry.execute(tool, {"part": "plate"}, context)
    calls = []
    registry.register(tool.transport, lambda t, a, c: calls.append((t, a, c)) or {"ok": True})
    with pytest.raises(AuthorizationError, match="not granted"):
        registry.execute(
            tool, {"part": "plate"}, context.model_copy(update={"allowed_tool_ids": set()})
        )
    with pytest.raises(ToolCatalogError, match="input schema"):
        registry.execute(tool, {"part": 123}, context)
    assert not calls
    result = registry.execute(tool, {"part": "plate"}, context)
    assert result.output == {"ok": True}
    assert result.run_id == context.run_id
    assert result.agent_id == context.agent_id
    assert result.actor_id == context.actor_id
    assert result.household_id == context.household_id
    assert result.invocation_id == context.invocation_id
    assert len(calls) == 1


def test_write_requires_action_policy_and_invalid_output_retains_unknown_outcome():
    tool = definition(side_effect=True, action_policy="write")
    registry = TransportRegistry()
    calls = []
    registry.register(tool.transport, lambda t, a, c: calls.append(a) or {"ok": "wrong"})
    with pytest.raises(AuthorizationError, match="action policy"):
        registry.execute(tool, {"part": "plate"}, execution_context(tool))
    assert not calls
    with pytest.raises(ToolExecutionError, match="output schema") as caught:
        registry.execute(
            tool, {"part": "plate"}, execution_context(tool, authorized_action="write")
        )
    assert caught.value.unknown
    assert len(calls) == 1


def test_unresolved_schema_reference_fails_before_execution():
    tool = definition(input_schema={"$ref": "#/missing"})
    registry = TransportRegistry()
    registry.register(tool.transport, lambda t, a, c: pytest.fail("unexpected execution"))
    with pytest.raises(ToolCatalogError, match="input schema"):
        registry.execute(tool, {"part": "plate"}, execution_context(tool))


def test_http_transport_sends_fixed_endpoint_environment_secret_and_trusted_context(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_TOOL_TOKEN", "synthetic-test-token")
    tool = definition(
        transport="http",
        endpoint="https://tools.example/action",
        credential_env="SYNTHETIC_TOOL_TOKEN",
    )
    context = execution_context(tool)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    registry = TransportRegistry()
    registry.register("http", HttpJsonTransport(transport=httpx.MockTransport(respond)))
    result = registry.execute(tool, {"part": "plate"}, context)
    assert result.output == {"ok": True}
    request = calls[0]
    assert str(request.url) == tool.endpoint
    assert request.headers["Authorization"] == "Bearer synthetic-test-token"
    assert request.headers["X-Actor-ID"] == str(context.actor_id)
    assert json.loads(request.content)["context"]["invocation_id"] == str(context.invocation_id)
    assert "synthetic-test-token" not in tool.model_dump_json()


@pytest.mark.parametrize("failure", ["timeout", "redirect", "server", "invalid", "oversize"])
def test_http_write_failure_is_bounded_sanitized_and_never_retried(failure):
    tool = definition(
        transport="http",
        endpoint="https://tools.example/action",
        side_effect=True,
        action_policy="write",
    )
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private endpoint details", request=request)
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://other.example"})
        if failure == "server":
            return httpx.Response(503, text="private server details")
        return httpx.Response(200, content=b"x" * (101 if failure == "oversize" else 1))

    registry = TransportRegistry()
    registry.register(
        "http",
        HttpJsonTransport(
            transport=httpx.MockTransport(respond),
            max_response_bytes=100,
        ),
    )
    with pytest.raises(ToolExecutionError) as caught:
        registry.execute(
            tool, {"part": "plate"}, execution_context(tool, authorized_action="write")
        )
    assert caught.value.unknown
    assert "private" not in str(caught.value)
    assert len(calls) == 1


def test_mcp_transport_uses_injected_session_and_preserves_execution_context():
    tool = definition(transport="mcp", settings={"tool_name": "arm_inspect"})
    context = execution_context(tool)
    calls = []

    class Client:
        def call_tool(self, name, arguments, *, context):
            calls.append((name, arguments, context))
            return {"ok": True}

    registry = TransportRegistry()
    registry.register("mcp", MCPTransport(Client()))
    assert registry.execute(tool, {"part": "plate"}, context).output == {"ok": True}
    assert calls == [("arm_inspect", {"part": "plate"}, context)]
