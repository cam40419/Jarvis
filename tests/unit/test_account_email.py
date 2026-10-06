"""UI delivery credentials remain encrypted and email transports require TLS."""

import json
from uuid import uuid4

import httpx
import pytest

from simon.adapters.account_email import AccountEmail
from simon.adapters.memory import InMemoryStore
from simon.config import Settings
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID, Membership
from simon.domain.integrations import ConnectIntegration
from simon.services.integrations import IntegrationService
from tests.unit.test_external_actions import actor


@pytest.fixture
def delivery(tmp_path):
    store = InMemoryStore()
    store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, workspace_id=DEV_WORKSPACE_ID, role="owner")
    )
    service = IntegrationService(
        store, Settings(_env_file=None, integration_key_file=tmp_path / "key")
    )
    owner = actor(scopes=frozenset({"identity:manage", "jobs:read", "jobs:write"}))
    return service, owner, AccountEmail(service)


def test_only_site_admin_owner_can_configure_email_and_secret_is_encrypted(delivery):
    service, owner, sender = delivery
    body = ConnectIntegration(credential="email-provider-secret", from_email="simon@example.com")
    with pytest.raises(AuthorizationError):
        service.connect(actor(), "email", body)
    with pytest.raises(AuthorizationError):
        service.connect(owner.model_copy(update={"actor_id": uuid4()}), "email", body)
    public = service.connect(owner, "email", body)
    assert sender.configured
    assert "email-provider-secret" not in json.dumps(public)
    assert "email-provider-secret" not in service.email_connection().model_dump_json()
    service.connect(owner, "email", body.model_copy(update={"credential": body.credential}))
    assert len(service.store.integration_connections(owner.workspace_id, owner.actor_id)) == 1


def test_resend_uses_fixed_https_endpoint_and_redacts_provider_errors(delivery, monkeypatch):
    service, owner, sender = delivery
    service.connect(
        owner,
        "email",
        ConnectIntegration(credential="resend-secret", from_email="simon@example.com"),
    )
    requests = []
    original_client = httpx.Client

    def handle(request):
        requests.append(request)
        return httpx.Response(400, text="provider-secret-details")

    monkeypatch.setattr(
        "simon.adapters.account_email.httpx.Client",
        lambda **kw: original_client(transport=httpx.MockTransport(handle), **kw),
    )
    with pytest.raises(ValidationError) as result:
        sender.send("owner@example.com", "Reset", "Your code is 12345678", uuid4())
    assert "provider-secret-details" not in str(result.value)
    assert str(requests[0].url) == "https://api.resend.com/emails"
    assert requests[0].headers["Authorization"] == "Bearer resend-secret"
    assert json.loads(requests[0].content)["to"] == ["owner@example.com"]


@pytest.mark.parametrize("port", [465, 587])
def test_smtp_encrypts_before_login(delivery, monkeypatch, port):
    service, owner, sender = delivery
    service.connect(
        owner,
        "email",
        ConnectIntegration(
            credential="smtp-secret",
            email_transport="smtp",
            from_email="simon@example.com",
            smtp_host="smtp.example.com",
            smtp_port=port,
            smtp_username="simon",
        ),
    )
    events = []

    class SMTP:
        def __init__(self, host, value, **kwargs):
            assert host == "smtp.example.com" and value == port
            if port == 465:
                assert kwargs["context"].check_hostname
                events.append("tls")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def starttls(self, *, context):
            assert context.check_hostname
            events.append("tls")

        def login(self, username, password):
            assert events == ["tls"]
            assert (username, password) == ("simon", "smtp-secret")
            events.append("login")

        def send_message(self, message):
            assert message["To"] == "owner@example.com"
            events.append("sent")

    monkeypatch.setattr("simon.adapters.account_email.smtplib.SMTP", SMTP)
    monkeypatch.setattr("simon.adapters.account_email.smtplib.SMTP_SSL", SMTP)
    sender.send("owner@example.com", "Verification", "12345678", uuid4())
    assert events == ["tls", "login", "sent"]
