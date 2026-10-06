"""Email recovery security across memory and PostgreSQL stores."""

import re
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from simon.api.app import AppContainer, create_app
from simon.config import Settings
from simon.domain.email_identity import ResetPasswordWithEmail
from simon.domain.errors import AuthenticationError, ValidationError
from simon.domain.identity import DEV_ACTOR_ID, Membership
from simon.domain.models import utc_now
from simon.services.email_identity import EmailIdentityService

ORIGIN = "http://localhost:8000"
PASSWORD = "the original private password"
NEW_PASSWORD = "the replacement private password"
EMAIL = "owner@example.com"


class Mailbox:
    configured = True

    def __init__(self):
        self.messages = []

    def send(self, recipient, subject, text, identifier):
        self.messages.append((recipient, subject, text, identifier))

    def code(self):
        return re.search(r"\b[0-9]{8}\b", self.messages[-1][2]).group()


@pytest.fixture
def recovery(store):
    settings = Settings(
        environment="test",
        storage_backend="memory",
        dev_login_enabled=True,
        dev_login_token=SecretStr("test-development-secret-32-characters"),
    )
    app = AppContainer(store=store, settings=settings)
    mailbox = Mailbox()
    service = EmailIdentityService(app.identity, mailbox)
    app.email_identity = service
    with TestClient(create_app(app), base_url=ORIGIN) as client:
        result = client.post(
            "/auth/dev-login",
            headers={"Origin": ORIGIN},
            json={"token": "test-development-secret-32-characters"},
        )
        token = client.cookies.get("simon_session")
        headers = {"Origin": ORIGIN, "X-CSRF-Token": result.json()["csrf_token"]}
        app.identity.set_password(token, "owner", PASSWORD, None)
        actor = app.identity.resolve(token)[1]
        yield client, app, service, mailbox, headers, actor


def verify(recovery):
    _client, app, service, mailbox, _headers, actor = recovery
    challenge = service.request_verification(actor, EMAIL.upper(), PASSWORD)
    assert app.store.password_for_email(EMAIL) is None
    assert service.confirm_verification(actor, challenge, mailbox.code()) == EMAIL
    return recovery


def reset_body(code):
    return {
        "email": EMAIL,
        "code": code,
        "password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
    }


def test_reset_email_is_generic_single_use_and_revokes_sessions(recovery):
    client, app, _service, mailbox, _headers, _actor = verify(recovery)
    known = client.post("/auth/password/forgot", headers={"Origin": ORIGIN}, json={"email": EMAIL})
    code = mailbox.code()
    unknown = client.post(
        "/auth/password/forgot", headers={"Origin": ORIGIN}, json={"email": "unknown@example.com"}
    )
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert len(mailbox.messages) == 2
    stored = app.store.email_codes(EMAIL, "reset", utc_now() - timedelta(hours=1))[0]
    assert stored.code_hash.startswith("$argon2id$") and code not in stored.code_hash
    response = client.post(
        "/auth/password/reset-email", headers={"Origin": ORIGIN}, json=reset_body(code)
    )
    assert response.status_code == 204, response.text
    assert client.get("/auth/session").status_code == 401
    assert (
        client.post(
            "/auth/password/reset-email", headers={"Origin": ORIGIN}, json=reset_body(code)
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/password/login",
            headers={"Origin": ORIGIN},
            json={"username": "owner", "password": PASSWORD},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/password/login",
            headers={"Origin": ORIGIN},
            json={"username": "owner", "password": NEW_PASSWORD},
        ).status_code
        == 200
    )
    assert app.store.password_for_actor(DEV_ACTOR_ID).email == EMAIL
    assert "password was reset" in mailbox.messages[-1][1]


def test_verification_requires_current_password_and_csrf(recovery):
    client, _app, _service, mailbox, headers, _actor = recovery
    body = {"email": EMAIL, "current_password": "incorrect password"}
    assert client.post("/auth/email/start", headers=headers, json=body).status_code == 401
    assert (
        client.post("/auth/email/start", headers={"Origin": ORIGIN}, json=body).status_code == 403
    )
    body["current_password"] = PASSWORD
    response = client.post("/auth/email/start", headers=headers, json=body)
    assert response.status_code == 200, response.text
    identifier = response.json()["challenge_id"]
    assert (
        client.post(
            "/auth/email/verify",
            headers=headers,
            json={"challenge_id": identifier, "code": mailbox.code()},
        ).status_code
        == 200
    )
    assert client.get("/auth/email").json()["email"] == EMAIL


def test_failed_attempts_commit_and_limit_code_guesses(recovery):
    _client, app, service, mailbox, _headers, _actor = verify(recovery)
    service.request_reset(EMAIL)
    actual = mailbox.code()
    wrong = "00000000" if actual != "00000000" else "11111111"
    for attempt in range(5):
        with pytest.raises(AuthenticationError):
            service.reset(ResetPasswordWithEmail(**reset_body(wrong)))
        assert (
            app.store.email_codes(EMAIL, "reset", utc_now() - timedelta(hours=1))[0].attempts
            == attempt + 1
        )
    with pytest.raises(AuthenticationError):
        service.reset(ResetPasswordWithEmail(**reset_body(actual)))


def test_expired_codes_and_password_mismatch_do_not_change_password(recovery):
    _client, app, service, mailbox, _headers, _actor = verify(recovery)
    service.request_reset(EMAIL)
    code = mailbox.code()
    body = reset_body(code)
    body["confirm_password"] = PASSWORD
    with pytest.raises(ValidationError):
        service.reset(ResetPasswordWithEmail(**body))
    stored = app.store.email_codes(EMAIL, "reset", utc_now() - timedelta(hours=1))[0]
    app.store.save_email_code(
        stored.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    )
    with pytest.raises(AuthenticationError):
        service.reset(ResetPasswordWithEmail(**reset_body(code)))
    assert app.identity.password_login("owner", PASSWORD, None)


def test_resend_throttle_and_verification_throttle_across_addresses(recovery):
    _client, _app, service, mailbox, _headers, actor = verify(recovery)
    service.request_reset(EMAIL)
    service.request_reset(EMAIL)
    assert len(mailbox.messages) == 2
    with pytest.raises(ValidationError):
        service.request_verification(actor, "another@example.com", PASSWORD)
    assert len(mailbox.messages) == 2


def test_missing_delivery_is_actionable_and_cross_origin_is_rejected(recovery):
    client, _app, _service, mailbox, _headers, _actor = recovery
    assert (
        client.post(
            "/auth/password/forgot",
            headers={"Origin": "https://evil.example"},
            json={"email": EMAIL},
        ).status_code
        == 403
    )
    mailbox.configured = False
    response = client.post(
        "/auth/password/forgot", headers={"Origin": ORIGIN}, json={"email": EMAIL}
    )
    assert response.status_code == 422
    assert "Connections" in response.text


def test_email_challenges_are_bound_to_account_and_email_is_unique(recovery):
    _client, app, service, mailbox, _headers, actor = recovery
    challenge = service.request_verification(actor, EMAIL, PASSWORD)
    foreign = actor.model_copy(update={"actor_id": uuid4()})
    app.store.put_membership(
        Membership(actor_id=foreign.actor_id, workspace_id=foreign.workspace_id, role="owner")
    )
    with pytest.raises(AuthenticationError):
        service.confirm_verification(foreign, challenge, mailbox.code())
    assert not app.store.email_code(challenge).consumed
    assert service.confirm_verification(actor, challenge, mailbox.code()) == EMAIL
    credential = app.store.password_for_actor(actor.actor_id)
    app.store.save_password(
        credential.model_copy(
            update={"actor_id": foreign.actor_id, "username": "other", "email": None}
        )
    )
    with pytest.raises(ValidationError):
        service.request_verification(foreign, EMAIL, PASSWORD)


def test_password_change_preserves_verified_email(recovery):
    client, app, _service, _mailbox, headers, _actor = verify(recovery)
    response = client.post(
        "/auth/password",
        headers=headers,
        json={"username": "owner", "password": NEW_PASSWORD, "current_password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert app.store.password_for_actor(DEV_ACTOR_ID).email == EMAIL
