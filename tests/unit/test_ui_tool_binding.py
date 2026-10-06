"""Connection readiness is actor-scoped, live and enforced again at dispatch."""

from uuid import uuid4

import httpx
import pytest

from simon.adapters.github_tools import GitHubTransport
from simon.adapters.memory import InMemoryStore
from simon.adapters.optional_http import BoundedHTTP
from simon.adapters.ui_tool_binding import with_ui_tools
from simon.agent_setup import starter_manifest
from simon.config import Settings
from simon.domain.errors import AuthorizationError
from simon.domain.integrations import ConnectIntegration
from simon.domain.tool_catalog import ToolCatalogError, ToolExecutionContext
from simon.services.agent_platform import AgentPlatformService
from simon.services.integrations import IntegrationService
from tests.unit.test_external_actions import actor


@pytest.fixture
def setup(tmp_path):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "test-owner"})
        return httpx.Response(200, json={"full_name": "example/allowed"})

    settings = Settings(_env_file=None, integration_key_file=tmp_path / "key")
    service = IntegrationService(
        InMemoryStore(), settings, http=BoundedHTTP(transport=httpx.MockTransport(handle))
    )
    platform = AgentPlatformService(
        service.store,
        starter_manifest(settings=settings),
        state_dir=tmp_path / "state",
        integrations=service,
        available_transports=("github", "browser"),
    )
    return service, platform, requests, handle


def test_missing_connection_has_actionable_setup_message(setup):
    _service, platform, requests, _handle = setup
    status = next(item for item in platform.tool_statuses(actor()) if item["id"] == "github.issue")
    assert status["state"] == "unconfigured"
    assert status["blocked_reasons"] == ["Connect GitHub in Connections to use this skill"]
    assert not requests


def test_saved_credential_is_private_live_and_never_shared_across_accounts(setup):
    service, platform, requests, handle = setup
    public = service.connect(
        actor(),
        "github",
        ConnectIntegration(credential="github-secret", repositories=["example/allowed"]),
    )
    assert "github-secret" not in str(public)
    status = {item["id"]: item for item in platform.tool_statuses(actor())}
    assert status["github.repository"]["state"] == "configured"
    assert status["github.issue_create"]["state"] == "unconfigured"
    foreign = actor(actor_id=uuid4())
    assert (
        next(item for item in platform.tool_statuses(foreign) if item["id"] == "github.repository")[
            "state"
        ]
        == "unconfigured"
    )
    tool = next(tool for tool in platform.manifest.tools if tool.id == "github.repository")
    context = ToolExecutionContext(
        actor_id=actor().actor_id,
        workspace_id=actor().workspace_id,
        run_id=uuid4(),
        agent_id="github",
        allowed_tool_ids=frozenset({tool.id}),
        scopes=actor().scopes,
        authorized_action="read",
    )
    transport = GitHubTransport(
        definition_resolver=platform.resolve_connected_tool,
        environ=service.credentials,
        transport=httpx.MockTransport(handle),
    )
    assert (
        transport(tool, {"repository": "example/allowed"}, context)["full_name"]
        == "example/allowed"
    )
    assert requests[-1].headers["Authorization"] == "Bearer github-secret"
    with pytest.raises(AuthorizationError):
        transport(tool, {"repository": "example/forbidden"}, context)
    service.disconnect(actor(), public["id"])
    with pytest.raises(ToolCatalogError, match="Connect GitHub"):
        transport(tool, {"repository": "example/allowed"}, context)


def test_writes_require_explicit_connection_setting_and_reconnect_is_live(setup):
    service, platform, _requests, _handle = setup
    body = ConnectIntegration(credential="github-secret", repositories=["example/allowed"])
    service.connect(actor(), "github", body)
    tool = next(tool for tool in platform.manifest.tools if tool.id == "github.issue_create")
    with pytest.raises(ToolCatalogError, match="Enable issue"):
        service.bind_github(tool, actor())
    service.connect(actor(), "github", body.model_copy(update={"github_write_enabled": True}))
    assert service.bind_github(tool, actor()).side_effect
    assert len(service.list(actor())) == 1


def test_screenshot_inherits_page_read_policy_without_widening_it():
    manifest = starter_manifest(settings=Settings(_env_file=None))
    reader = next(tool for tool in manifest.tools if tool.id == "browser.read")
    reader = reader.model_copy(
        update={
            "enabled": True,
            "configured": True,
            "settings": {"allowed_origins": ["https://example.com"], "public_web": False},
        }
    )
    manifest = manifest.model_copy(
        update={"tools": tuple(reader if t.id == reader.id else t for t in manifest.tools)}
    )
    updated = with_ui_tools(manifest)
    screenshot = next(tool for tool in updated.tools if tool.id == "browser.screenshot")
    assert screenshot.enabled and screenshot.configured
    assert screenshot.settings == reader.settings
    assert screenshot.side_effect and screenshot.required_scopes == frozenset({"jobs:write"})
    assert not any(tool.id == "browser.interact" for tool in updated.tools)


def test_legacy_extension_placeholders_do_not_advertise_installed_integrations(setup):
    from simon.services.tool_catalog import builtin_tool_templates

    service, platform, requests, _handle = setup
    templates = builtin_tool_templates()
    legacy = platform.manifest.model_copy(
        update={"tools": (*platform.manifest.tools, *templates)}
    )
    updated = AgentPlatformService(
        service.store,
        legacy,
        state_dir=platform.state_dir,
        integrations=service,
        available_transports=("github", "browser"),
    )
    template_ids = {tool.id for tool in templates}
    assert not template_ids.intersection(tool.id for tool in updated.manifest.tools)
    catalog = updated.catalog(actor())
    assert not template_ids.intersection(tool["id"] for tool in catalog["tool_statuses"])
    assert any(tool["id"] == "github.repository" for tool in catalog["tool_statuses"])
    assert with_ui_tools(updated.manifest) == updated.manifest
    assert not requests


def test_customized_extension_contracts_are_preserved():
    from simon.services.tool_catalog import builtin_tool_templates

    manifest = starter_manifest(Settings(_env_file=None))
    template = next(tool for tool in builtin_tool_templates() if tool.id == "data.query")
    customized = template.model_copy(
        update={
            "enabled": True,
            "configured": True,
            "endpoint": "https://example.com/mcp",
            "settings": {"tool_name": "query"},
        }
    )
    manifest = manifest.model_copy(update={"tools": (*manifest.tools, customized)})
    updated = with_ui_tools(manifest)
    assert next(tool for tool in updated.tools if tool.id == customized.id) == customized


def test_new_installations_include_real_tool_contracts_instead_of_extension_examples():
    from simon.services.tool_catalog import builtin_tool_templates

    manifest = starter_manifest(Settings(_env_file=None))
    assert not {tool.id for tool in manifest.tools}.intersection(
        tool.id for tool in builtin_tool_templates()
    )
    assert {"cad.openscad_export", "pcb.erc", "generative.image_generate"} <= {
        tool.id for tool in manifest.tools
    }


def test_explicit_operator_repository_grants_are_preserved():
    from simon.adapters.github_tools import github_tool_definitions

    manifest = starter_manifest(settings=Settings(_env_file=None))
    legacy = github_tool_definitions(
        enabled=False,
        workspace_id=actor().workspace_id,
        actor_ids=(actor().actor_id,),
        repositories=("example/allowed",),
    )
    definitions = {tool.id: tool for tool in legacy}
    manifest = manifest.model_copy(
        update={"tools": tuple(definitions.get(t.id, t) for t in manifest.tools)}
    )
    updated = with_ui_tools(manifest)
    tool = next(tool for tool in updated.tools if tool.id == "github.repository")
    assert not tool.enabled and tool.settings["repositories"] == ["example/allowed"]


def test_invalid_repository_and_bad_token_never_save_a_connection(setup):
    service, _platform, _requests, _handle = setup
    from simon.domain.errors import ValidationError

    with pytest.raises(ValidationError):
        service.connect(
            actor(),
            "github",
            ConnectIntegration(credential="github-secret", repositories=["../allowed"]),
        )
    service.clickup.http.transport = httpx.MockTransport(
        lambda request: httpx.Response(401, text="github-secret")
    )
    with pytest.raises(ValidationError, match="check your access token"):
        service.connect(
            actor(),
            "github",
            ConnectIntegration(credential="github-secret", repositories=["example/allowed"]),
        )
    assert not service.list(actor())
