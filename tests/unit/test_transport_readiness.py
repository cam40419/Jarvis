"""Offline configuration gates cannot hide network effects or invalid tool protocols."""

from uuid import uuid4

import pytest

from simon.adapters.cad_tools import cad_tool_definitions
from simon.adapters.generative_tools import generative_tool_definitions
from simon.adapters.pcb_tools import pcb_tool_definitions
from simon.adapters.tool_preflight import integration_status, uses_network
from simon.domain.models import ActorContext, Channel
from simon.domain.tool_catalog import ToolDefinition


@pytest.fixture
def actor():
    return ActorContext(
        actor_id=uuid4(),
        workspace_id=uuid4(),
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )


@pytest.mark.parametrize(
    "transport",
    [
        "http",
        "mcp",
        "github",
        "webdav",
        "dropbox",
        "box",
        "onedrive",
        "generative",
        "web_research",
        "application",
    ],
)
def test_network_transports_cannot_omit_the_network_declaration(transport):
    tool = ToolDefinition(id="test.operation", description="An operation", transport=transport)
    assert uses_network(tool)


@pytest.mark.parametrize(
    "identifier,transport,network",
    [
        ("browser.read", "browser", True),
        ("browser.screenshot", "browser", True),
        ("browser.render_html", "browser", False),
        ("external_actions.quote", "external_actions", True),
        ("external_actions.list", "external_actions", False),
        ("local.operation", "native", False),
    ],
)
def test_local_rendering_and_metadata_are_distinct_from_network_effects(
    identifier, transport, network
):
    tool = ToolDefinition(id=identifier, description="An operation", transport=transport)
    assert uses_network(tool) is network
    assert uses_network(tool.model_copy(update={"settings": {"network": True}}))


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"endpoint": "https://tools.example.test"},
        {
            "endpoint": "https://tools.example.test",
            "settings": {"tool_name": "read", "protocol_version": "unknown"},
        },
    ],
)
def test_mcp_readiness_requires_an_explicit_endpoint_tool_and_supported_protocol(actor, updates):
    tool = ToolDefinition(id="mcp.read", description="Read", transport="mcp").model_copy(
        update=updates
    )
    state, reasons = integration_status(tool, actor, {})
    assert state == "unconfigured" and reasons
    configured = tool.model_copy(
        update={"endpoint": "https://tools.example.test", "settings": {"tool_name": "read"}}
    )
    assert integration_status(configured, actor, {}) == ("configured", ())


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"allowed_origins": ["http://example.test"]},
        {"allowed_origins": ["https://example.test/path"]},
    ],
)
def test_browser_network_read_requires_exact_https_origins_or_public_web(actor, settings):
    tool = ToolDefinition(
        id="browser.read", description="Read", transport="browser", settings=settings
    )
    assert integration_status(tool, actor, {})[0] == "unconfigured"
    ready = tool.model_copy(update={"settings": {"allowed_origins": ["https://example.test"]}})
    assert integration_status(ready, actor, {}) == ("configured", ())
    public = tool.model_copy(update={"settings": {"public_web": True}})
    assert integration_status(public, actor, {}) == ("configured", ())
    unknown = tool.model_copy(update={"id": "browser.execute"})
    assert integration_status(unknown, actor, {})[0] == "unconfigured"


@pytest.mark.parametrize(
    "tools",
    [
        cad_tool_definitions(),
        pcb_tool_definitions(),
        generative_tool_definitions(
            image_model="synthetic-image", transcription_model="synthetic-audio"
        ),
    ],
)
def test_execution_transport_grants_are_checked_offline_before_admission(actor, tools):
    for tool in tools:
        assert integration_status(tool, actor, {}) == ("configured", ())
        stripped = tool.model_copy(update={"environment_capabilities": frozenset()})
        state, reasons = integration_status(stripped, actor, {})
        assert state == "unconfigured" and reasons
