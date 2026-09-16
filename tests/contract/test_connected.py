import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from time import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from simon.adapters.google import CALENDAR_SCOPE, EMAIL_SCOPE, ConnectedError, GoogleTokens
from simon.config import Settings
from simon.domain.conversations import CreateThread, SubmitRun
from simon.domain.errors import (
    AuthenticationError,
    AuthorizationError,
    ModelError,
    NotFoundError,
    ValidationError,
)
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership
from simon.domain.models import utc_now
from simon.services.audit import AuditService
from simon.services.connected import ConnectedService
from simon.services.identity import IdentityService
from simon.services.interaction import InteractionService
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_model_runs import FakeModel

EMAIL = {"to": "test@example.com", "subject": "Reservation request", "body": "A synthetic test."}
EVENT = {"title": "Lunch", "start": "2026-10-01T12:00:00-04:00", "end": "2026-10-01T13:00:00-04:00"}


@pytest.fixture
def setup(store):
    return connected_setup(store)


def connected_setup(store):
    settings = Settings(
        _env_file=None,
        dev_login_enabled=True,
        dev_login_token=SecretStr("test-development-secret-32-characters"),
        google_client_id="client.apps.googleusercontent.com",
        google_client_secret=SecretStr("secret"),
        google_token_key=SecretStr(Fernet.generate_key().decode()),
    )
    store.put_membership(
        Membership(
            actor_id=DEV_ACTOR_ID,
            household_id=DEV_HOUSEHOLD_ID,
            role="owner",
            display_name="Test",
            household_name="Test",
        )
    )
    identity = IdentityService(store, settings)
    token, _ = identity.development_login(settings.dev_login_token.get_secret_value(), None)
    _, actor = identity.resolve(token)
    service = ConnectedService(store, AuditService(store), settings, identity)
    tokens = GoogleTokens(
        access_token="access-secret", refresh_token="refresh-secret", expires_at=time() + 3600
    )
    service.api.exchange = lambda *args: (tokens, (CALENDAR_SCOPE, EMAIL_SCOPE))
    service.api.account_email = lambda *args: "owner@example.com"
    url, binding = service.start(actor, token)
    state = parse_qs(urlsplit(url).query)["state"][0]
    service.callback(state, binding, "code")
    return service, actor, token


def make_proposal(service, actor, kind="propose_email"):
    model = FakeModel()

    def generate(request, on_delta, execute):
        result = json.loads(execute(kind, json.dumps(EMAIL if kind == "propose_email" else EVENT)))
        assert result["requires_confirmation"] and not result["executed"]
        return model.generate(request)

    model.generate_with_tools = generate
    conversations = ModelConversationService(
        service.store, service.audit, model, service.settings, service
    )
    thread = conversations.create(actor, CreateThread(title="Tools", idempotency_key=str(uuid4())))
    run = conversations.submit(
        actor, thread.id, SubmitRun(text="Prepare a test", idempotency_key=str(uuid4()))
    )
    return service.get_action(actor, run.action_ids[0]), thread, run


def test_oauth_binding_single_use_encryption_and_scopes(setup):
    service, actor, token = setup
    assert service.status(actor)["connected"]
    connection = service.connection(actor)
    assert "access-secret" not in connection.model_dump_json()
    assert "refresh-secret" not in str(service.store.audit_events())
    assert service.available(actor) == (
        "web_search",
        "calendar_list_events",
        "propose_calendar_event",
        "propose_email",
    )
    url, binding = service.start(actor, token)
    params = parse_qs(urlsplit(url).query)
    assert params["code_challenge_method"] == ["S256"] and params["access_type"] == ["offline"]
    with pytest.raises(ValidationError):
        service.callback(params["state"][0], "wrong-browser", "code")
    service.callback(params["state"][0], binding, "code")
    with pytest.raises(ValidationError):
        service.callback(params["state"][0], binding, "code")
    service.disconnect(actor)
    assert service.available(actor) == ("web_search",)
    assert not service.status(actor)["connected"]


def test_oauth_revoked_session_cannot_connect(setup):
    service, actor, token = setup
    url, binding = service.start(actor, token)
    service.store.revoke_sessions(actor.actor_id)
    service.disconnect(actor)
    with pytest.raises(AuthenticationError):
        service.callback(parse_qs(urlsplit(url).query)["state"][0], binding, "code")
    assert not service.status(actor)["connected"]


@pytest.mark.parametrize("kind", ["propose_email", "propose_calendar_event"])
def test_preview_confirmation_is_durable_scoped_and_once(setup, kind):
    service, actor, _ = setup
    sent = []
    service.api.execute = lambda token, action, sender: (
        sent.append(action) or "receipt",
        "https://www.google.com/calendar/event",
    )
    action, thread, run = make_proposal(service, actor, kind)
    assert not sent and action.status == "pending"
    answers = InteractionService(service.store, service.audit).answers(actor, thread.id)
    assert answers[0].actions == (action,)
    assert run.capability_manifest == service.available(actor)
    other = actor.model_copy(update={"actor_id": uuid4()})
    assert not InteractionService(service.store, service.audit).answers(other, thread.id)[0].actions
    with pytest.raises(NotFoundError):
        service.get_action(other, action.id)
    result = service.decide(actor, action.id, confirm=True, revalidate=lambda: actor)
    assert result.status == "succeeded" and len(sent) == 1
    assert sent[0].email == action.email and sent[0].calendar == action.calendar
    assert service.decide(actor, action.id, confirm=True, revalidate=lambda: actor) == result
    assert len(sent) == 1
    # A fresh adapter sees the same receipt (including the PostgreSQL contract).
    assert (
        ConnectedService(
            service.store, service.audit, service.settings, service.identity
        ).get_action(actor, action.id)
        == result
    )


@pytest.mark.parametrize(
    "outcome", ["cancel", "expire", "disconnect", "different_account", "revoked"]
)
def test_ineligible_previews_never_dispatch(setup, outcome):
    service, actor, _ = setup
    action, _, _ = make_proposal(service, actor)
    service.api.execute = lambda *args: pytest.fail("must not dispatch")
    if outcome == "expire":
        service.store.save_action(
            action.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
        )
    if outcome == "disconnect":
        service.disconnect(actor)
    if outcome == "different_account":
        service.store.save_google_connection(
            service.connection(actor).model_copy(update={"id": uuid4()})
        )
    checked = actor.model_copy(update={"scopes": frozenset()}) if outcome == "revoked" else actor
    if outcome in {"disconnect", "different_account", "revoked"}:
        with pytest.raises((ConnectedError, AuthorizationError)):
            service.decide(actor, action.id, confirm=True, revalidate=lambda: checked)
    else:
        assert (
            service.decide(
                actor, action.id, confirm=outcome != "cancel", revalidate=lambda: actor
            ).status
            == "cancelled"
        )


@pytest.mark.parametrize("unknown", [True, False])
def test_failed_or_unknown_dispatch_is_not_retried(setup, unknown):
    service, actor, _ = setup
    action, _, _ = make_proposal(service, actor)
    calls = []

    def fail(*args):
        calls.append(1)
        raise ConnectedError("Network problem", unknown=unknown)

    service.api.execute = fail
    result = service.decide(actor, action.id, confirm=True, revalidate=lambda: actor)
    assert result.status == ("unknown" if unknown else "failed")
    assert service.decide(actor, action.id, confirm=True, revalidate=lambda: actor) == result
    assert calls == [1]


def test_concurrent_confirm_releases_transaction_and_never_duplicates(setup):
    service, actor, _ = setup
    action, _, _ = make_proposal(service, actor)
    entered, release = Event(), Event()

    def hold(*args):
        entered.set()
        assert release.wait(10)
        return "receipt", None

    service.api.execute = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            service.decide, actor, action.id, confirm=True, revalidate=lambda: actor
        )
        assert entered.wait(10)
        try:
            second = pool.submit(
                service.decide, actor, action.id, confirm=True, revalidate=lambda: actor
            )
            assert second.result(timeout=5).status == "executing"
        finally:
            release.set()
        assert first.result(timeout=5).status == "succeeded"


def test_tool_validation_reads_and_cancelled_generation(setup):
    service, actor, _ = setup
    previews = []
    execute = service.executor(actor, uuid4(), previews, lambda: actor)
    for arguments in [
        "not json",
        json.dumps({**EMAIL, "subject": "Subject\r\nBcc: hidden@example.com"}),
        "x" * 20001,
    ]:
        assert "error" in json.loads(execute("propose_email", arguments))
    service.api.events = lambda token, query: {"events": [], "timezone": "America/New_York"}
    result = execute(
        "calendar_list_events", json.dumps({"start": EVENT["start"], "end": EVENT["end"]})
    )
    assert json.loads(result)["events"] == [] and not previews
    for _ in range(3):
        assert "preview_id" in json.loads(execute("propose_email", json.dumps(EMAIL)))
    assert "error" in json.loads(execute("propose_email", json.dumps(EMAIL)))
    with pytest.raises(AuthorizationError):
        execute("send_email", json.dumps(EMAIL))
    assert all(service.store.action(p.id) is None for p in previews)
    model = FakeModel()
    made = []

    def fail(request, delta, execute):
        made.append(json.loads(execute("propose_email", json.dumps(EMAIL)))["preview_id"])
        raise ModelError("model_cancelled")

    model.generate_with_tools = fail
    conversations = ModelConversationService(
        service.store, service.audit, model, service.settings, service
    )
    thread = conversations.create(
        actor, CreateThread(title="Cancelled", idempotency_key=str(uuid4()))
    )
    with pytest.raises(ModelError):
        conversations.submit(
            actor, thread.id, SubmitRun(text="Email test", idempotency_key=str(uuid4()))
        )
    from uuid import UUID

    assert service.store.action(UUID(made[0])) is None


def test_refresh_preserves_binding_and_disconnect_during_refresh(setup):
    service, actor, _ = setup
    connection = service.connection(actor)
    expired = GoogleTokens(access_token="old", refresh_token="refresh", expires_at=0)
    connection = connection.model_copy(
        update={"encrypted_tokens": service.encrypt(expired.model_dump_json())}
    )
    service.store.save_google_connection(connection)
    refreshed = expired.model_copy(update={"access_token": "new", "expires_at": time() + 3600})
    service.api.refresh = lambda tokens: refreshed
    assert service.access_token(actor, connection) == "new"
    assert service.connection(actor).id == connection.id

    def disconnect(tokens):
        service.disconnect(actor)
        return refreshed

    service.api.refresh = disconnect
    with pytest.raises(ConnectedError):
        service.access_token(actor, connection)
    assert not service.status(actor)["connected"]


def test_connected_storage_transaction_rolls_back(setup):
    service, actor, _ = setup
    before = service.connection(actor)
    with pytest.raises(RuntimeError), service.store.transaction(actor.household_id):
        service.store.delete_google_connection(actor.household_id, actor.actor_id)
        raise RuntimeError("rollback")
    assert service.connection(actor) == before
