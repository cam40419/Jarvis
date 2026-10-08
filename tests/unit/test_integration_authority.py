"""Current account grants, read-only readiness and cross-process credential revocation."""

import base64
import json
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from simon.adapters.cloud_storage_tools import dropbox_tool_definitions
from simon.adapters.github_tools import github_tool_definitions
from simon.adapters.memory import InMemoryStore
from simon.adapters.optional_http import BoundedHTTP
from simon.config import Settings
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.identity import Membership
from simon.domain.integrations import ConnectIntegration
from simon.domain.models import ActorContext, Channel
from simon.domain.tool_catalog import ToolCatalogError
from simon.services.integrations import IntegrationCredentials, IntegrationService

SECRET = "synthetic-provider-secret"


@pytest.fixture
def integration(tmp_path, monkeypatch):
    from simon.services import credentials

    monkeypatch.setattr(credentials, "runtime_credentials", lambda: {"SYNTHETIC": "fallback"})
    actor = ActorContext(
        actor_id=uuid4(),
        workspace_id=uuid4(),
        scopes=frozenset({"jobs:read", "jobs:write", "identity:manage"}),
        channel=Channel.API,
    )
    store = InMemoryStore()
    store.put_membership(
        Membership(actor_id=actor.actor_id, workspace_id=actor.workspace_id, role="owner")
    )
    settings = Settings(
        _env_file=None,
        account_admin_actor_id=actor.actor_id,
        account_workspace_id=actor.workspace_id,
        google_token_key=SecretStr(Fernet.generate_key().decode()),
        integration_key_file=tmp_path / "credentials.key",
    )
    requests = []

    def send(request):
        requests.append(request)
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "synthetic-owner"})
        if request.url.path == "/api/v2/team":
            return httpx.Response(200, json={"teams": [{"id": "123", "name": "Company"}]})
        return httpx.Response(207 if request.method == "PROPFIND" else 200, json={})

    service = IntegrationService(
        store, settings, http=BoundedHTTP(transport=httpx.MockTransport(send))
    )
    return service, actor, requests


def body(provider, **changes):
    values = {
        "credential": "pk_synthetic" if provider == "clickup" else SECRET,
        **{
            "github": {"repositories": ["example/allowed"]},
            "dropbox": {"root_path": "/approved"},
            "box": {"root_folder_id": "123"},
            "onedrive": {
                "drive_id": "drive-a",
                "root_item_id": "root-a",
                "download_hosts": ["tenant.sharepoint.com"],
            },
            "webdav": {"endpoint": "https://files.example.test/approved", "username": "worker"},
            "twilio": {"account_sid": "AC" + "a" * 32, "from_number": "+15551234567"},
            "gateway": {
                "endpoint": "https://booking.example.test",
                "merchant_names": {"shop": "Shop"},
            },
            "home": {"endpoint": "https://home.example.test"},
            "google_app": {"client_id": "synthetic.apps.googleusercontent.com"},
            "email": {"from_email": "service@example.test"},
        }.get(provider, {}),
        **changes,
    }
    return ConnectIntegration(**values)


def managed(definition):
    return definition.model_copy(update={"settings": {"ui_managed": True, "network": True}})


def test_managed_storage_grants_are_rebound_and_disconnection_reaches_workers(integration):
    service, actor, requests = integration
    templates = {tool.id: managed(tool) for tool in dropbox_tool_definitions()}
    public = service.connect(actor, "dropbox", body("dropbox"))
    reader = service.bind_tool(templates["dropbox.read"], actor)
    assert reader.settings["root_path"] == "/approved"
    assert reader.settings["workspace_id"] == str(actor.workspace_id)
    assert reader.settings["actor_ids"] == [str(actor.actor_id)]
    assert service.credentials[reader.credential_env] == SECRET
    assert SECRET not in json.dumps(public)
    with pytest.raises(ToolCatalogError, match="Enable file writes"):
        service.bind_tool(templates["dropbox.upload"], actor)
    for stranger in (
        actor.model_copy(update={"actor_id": uuid4()}),
        actor.model_copy(update={"workspace_id": uuid4()}),
    ):
        with pytest.raises(ToolCatalogError, match="Connect Dropbox"):
            service.bind_tool(templates["dropbox.read"], stranger)

    service.connect(
        actor,
        "dropbox",
        body("dropbox", credential="rotated-secret", root_path="/new", storage_write_enabled=True),
        public["id"],
    )
    assert service.credentials[reader.credential_env] == "rotated-secret"
    writer = service.bind_tool(templates["dropbox.upload"], actor)
    assert writer.settings["root_path"] == "/new" and writer.side_effect
    count = len(requests)
    service.disconnect(actor, public["id"])
    assert service.credentials.get(reader.credential_env) is None
    with pytest.raises(ToolCatalogError, match="Connect Dropbox"):
        service.bind_tool(templates["dropbox.read"], actor)
    assert len(requests) == count


@pytest.mark.parametrize("provider", ["dropbox", "github"])
def test_failed_readiness_probe_blocks_binding_until_reconnected(integration, provider):
    service, actor, _ = integration
    public = service.connect(actor, provider, body(provider))
    definitions = dropbox_tool_definitions() if provider == "dropbox" else github_tool_definitions()
    definition = managed(next(tool for tool in definitions if not tool.side_effect))
    service.clickup.http.transport = httpx.MockTransport(lambda _: httpx.Response(401, text=SECRET))
    result = service.test(actor, public["id"])
    assert result["status"] == "failed" and SECRET not in json.dumps(result)
    with pytest.raises(ToolCatalogError, match="failed its last test"):
        service.bind_tool(definition, actor)


@pytest.mark.parametrize(
    ("provider", "method", "path"),
    [
        ("openai", "GET", "/v1/models"),
        ("github", "GET", "/repos/example/allowed"),
        ("twilio", "GET", "/2010-04-01/Accounts/" + "AC" + "a" * 32 + ".json"),
        ("home", "GET", "/health"),
        ("gateway", "GET", "/health"),
        ("clickup", "GET", "/api/v2/team"),
        ("dropbox", "POST", "/2/files/get_metadata"),
        ("box", "GET", "/2.0/folders/123"),
        ("onedrive", "GET", "/v1.0/drives/drive-a/items/root-a"),
        ("webdav", "PROPFIND", "/approved/"),
    ],
)
def test_readiness_uses_only_bounded_account_or_resource_reads(integration, provider, method, path):
    service, actor, requests = integration
    public = service.connect(actor, provider, body(provider))
    requests.clear()
    result = service.test(actor, public["id"])
    assert result["status"] == "passed"
    assert [(request.method, request.url.path) for request in requests] == [(method, path)]
    if provider == "twilio":
        value = ("AC" + "a" * 32 + ":" + SECRET).encode()
        assert requests[0].headers["authorization"] == "Basic " + base64.b64encode(value).decode()
    assert SECRET not in json.dumps(service.list(actor))


@pytest.mark.parametrize("provider", ["google_app", "email"])
def test_configuration_only_does_not_claim_provider_validation(integration, provider):
    service, actor, requests = integration
    public = service.connect(actor, provider, body(provider))
    assert service.test(actor, public["id"])["status"] == "configuration_only"
    assert requests == []


@pytest.mark.parametrize("change", ["rotated", "disconnected"])
def test_probe_result_cannot_revalidate_a_changed_connection(integration, change):
    service, actor, _ = integration
    public = service.connect(actor, "openai", body("openai"))

    def change_during_probe(_):
        if change == "rotated":
            service.connect(actor, "openai", body("openai", credential="replacement-secret"))
        else:
            service.disconnect(actor, public["id"])
        return httpx.Response(200, json={})

    service.clickup.http.transport = httpx.MockTransport(change_during_probe)
    if change == "rotated":
        assert service.test(actor, public["id"])["status"] == "configuration_changed"
        assert "connection_test" not in service.list(actor)[0]["settings"]
        assert service.credentials["SIMON_OPENAI_API_KEY"] == "replacement-secret"
    else:
        with pytest.raises(NotFoundError, match="disconnected while its test"):
            service.test(actor, public["id"])
        assert service.openai_connection() is None


def test_shared_key_resolution_is_admin_owned_and_rotatable_without_exposing_records(integration):
    service, actor, _ = integration
    worker = IntegrationCredentials(service, {"FALLBACK": "value"})
    with pytest.raises(KeyError):
        worker["SIMON_OPENAI_API_KEY"]
    public = service.connect(actor, "openai", body("openai"))
    assert worker["SIMON_OPENAI_API_KEY"] == SECRET
    assert dict(worker) == {"FALLBACK": "value"} and len(worker) == 1
    assert (
        public["id"]
        == service.connect(actor, "openai", body("openai", credential="rotated-secret"))["id"]
    )
    assert worker["SIMON_OPENAI_API_KEY"] == "rotated-secret"
    service.settings.account_workspace_id = uuid4()
    assert service.openai_connection() is None
    service.settings.account_workspace_id = None
    assert worker["SIMON_OPENAI_API_KEY"] == "rotated-secret"
    for key in ("SIMON_LINK_invalid", "SIMON_LINK_bad_bad_link_missing"):
        with pytest.raises(KeyError):
            worker[key]


@pytest.mark.parametrize("provider", ["google_app", "openai", "email"])
def test_shared_application_connections_require_site_administrator(integration, provider):
    service, actor, requests = integration
    stranger = actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(AuthorizationError, match="site administrator"):
        service.connect(stranger, provider, body(provider))
    assert service.list(actor) == [] and requests == []


def test_connection_reads_and_writes_require_current_calling_scopes(integration):
    service, actor, requests = integration
    with pytest.raises(AuthorizationError, match="not authorized"):
        service.list(actor.model_copy(update={"scopes": frozenset()}))
    with pytest.raises(AuthorizationError, match="not authorized"):
        service.connect(
            actor.model_copy(update={"scopes": frozenset({"jobs:read"})}), "openai", body("openai")
        )
    with pytest.raises(AuthorizationError, match="authenticated UI"):
        service.connect(
            actor.model_copy(update={"channel": Channel.WORKER}), "openai", body("openai")
        )
    assert requests == []


@pytest.mark.parametrize(
    ("provider", "changes", "message"),
    [
        ("unsupported", {}, "Unsupported"),
        ("openai", {"credential": "unsafe secret"}, "valid provider credential"),
        ("clickup", {"credential": "wrong-prefix"}, "ClickUp personal"),
        ("email", {"from_email": None}, "sender email"),
        ("email", {"email_transport": "smtp"}, "SMTP server"),
        ("github", {"repositories": ["../forbidden"]}, "permitted GitHub"),
        ("webdav", {"username": None}, "WebDAV username"),
        ("home", {"endpoint": "http://public.example.test"}, "HTTPS or local"),
        ("google_app", {"client_id": "not-a-google-client"}, "OAuth client"),
    ],
)
def test_invalid_connection_contract_is_rejected_before_external_probe(
    integration, provider, changes, message
):
    service, actor, requests = integration
    with pytest.raises(ValidationError, match=message):
        service.connect(actor, provider, body(provider, **changes))
    assert service.list(actor) == [] and requests == []


def test_lost_or_replaced_encryption_key_never_returns_garbage_secrets(integration):
    service, actor, _ = integration
    service.connect(actor, "openai", body("openai"))
    service.settings.google_token_key = SecretStr(Fernet.generate_key().decode())
    with pytest.raises(ValidationError, match="Restore the connection encryption key"):
        service.credentials["SIMON_OPENAI_API_KEY"]
    service.settings.google_token_key = None
    service.settings.integration_key_file.write_bytes(b"not-a-fernet-key")
    with pytest.raises(ValidationError, match="encryption key is unavailable"):
        service.credentials["SIMON_OPENAI_API_KEY"]


@pytest.mark.parametrize("port", [465, 587])
def test_smtp_readiness_authenticates_over_tls_without_sending_email(
    integration, monkeypatch, port
):
    from simon.services import integrations

    service, actor, requests = integration
    events = []

    class SMTP:
        def __init__(self, host, selected_port, *, timeout, context=None):
            assert host == "smtp.example.test" and selected_port == port and timeout == 10
            assert (context is not None) == (port == 465)
            events.append("connected")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            events.append("closed")

        def starttls(self, *, context):
            assert context.check_hostname
            events.append("tls")

        def login(self, username, password):
            assert username == "service" and password == SECRET
            events.append("authenticated")

        def sendmail(self, *args, **kwargs):
            pytest.fail("Readiness must not send email")

    monkeypatch.setattr(integrations.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(integrations.smtplib, "SMTP_SSL", SMTP)
    public = service.connect(
        actor,
        "email",
        body(
            "email",
            email_transport="smtp",
            smtp_host="smtp.example.test",
            smtp_port=port,
            smtp_username="service",
        ),
    )
    assert service.email_connection().id == public["id"]
    assert service.test(actor, public["id"])["status"] == "passed"
    assert events == ["connected", *(["tls"] if port == 587 else []), "authenticated", "closed"]
    assert requests == []
    without_admin = actor.model_copy(update={"scopes": frozenset({"jobs:read", "jobs:write"})})
    with pytest.raises(AuthorizationError, match="site administrator"):
        service.test(without_admin, public["id"])


@pytest.mark.parametrize(
    "response", [httpx.Response(401, text=SECRET), httpx.Response(200, json=[])]
)
def test_invalid_github_probe_cannot_save_a_connection(integration, response):
    service, actor, _ = integration
    service.clickup.http.transport = httpx.MockTransport(lambda _: response)
    with pytest.raises(ValidationError, match="GitHub") as error:
        service.connect(actor, "github", body("github"))
    assert SECRET not in str(error.value)
    assert service.list(actor) == []


def test_clickup_resource_lookup_cannot_cross_account_or_workspace(integration):
    service, actor, requests = integration
    public = service.connect(actor, "clickup", body("clickup"))
    selected = service.board_connection(actor, public["id"] + "_123")
    assert selected is not None and selected.clickup_workspace_id == "123"
    assert (
        service.board_connection(actor.model_copy(update={"workspace_id": uuid4()}), selected.id)
        is None
    )
    assert (
        service.board_connection(actor.model_copy(update={"actor_id": uuid4()}), selected.id)
        is None
    )
    before = len(requests)
    assert service.board_connections(actor) == service.board_connections(actor)
    assert len(requests) == before + 1
    service.disconnect(actor, public["id"])
    assert service.board_connection(actor, selected.id) is None
    assert service.board_connections(actor) == ()


def test_unknown_connection_cannot_be_probed_and_unmanaged_definition_stays_explicit(integration):
    service, actor, requests = integration
    with pytest.raises(NotFoundError, match="Connection not found"):
        service.test(actor, "link_missing")
    definition = github_tool_definitions()[0]
    assert service.bind_tool(definition, actor) is definition
    assert service.bind_github(definition, actor) is definition
    assert requests == []
