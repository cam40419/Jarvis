from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.api.app import AppContainer, create_app
from jarvis.config import Settings
from jarvis.domain.errors import AuthenticationError
from jarvis.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership
from jarvis.domain.models import utc_now
from jarvis.services.identity import IdentityService, token_hash
from tests.passkey_helper import SoftwarePasskey

ORIGIN = "http://localhost:8000"
SECRET = "identity-test-development-secret-32"


@pytest.fixture
def identity_app(store):
    settings = Settings(
        environment="test",
        storage_backend="memory",
        dev_login_enabled=True,
        dev_login_token=SecretStr(SECRET),
    )
    return AppContainer(store=store, settings=settings)


@pytest.fixture
def browser(identity_app):
    with TestClient(create_app(identity_app), base_url=ORIGIN) as client:
        yield client


def login(browser):
    response = browser.post("/auth/dev-login", headers={"Origin": ORIGIN}, json={"token": SECRET})
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def register(browser, app, key=None):
    key = key or SoftwarePasskey()
    invitation = app.identity.enroll(DEV_ACTOR_ID, DEV_HOUSEHOLD_ID)
    start = browser.post(
        "/auth/passkeys/register/options", headers={"Origin": ORIGIN}, json={"token": invitation}
    )
    assert start.status_code == 200
    flow = start.json()
    credential = key.response(flow["options"], registration=True)
    response = browser.post(
        "/auth/passkeys/register/verify",
        headers={"Origin": ORIGIN},
        json={"ceremony_id": flow["ceremony_id"], "credential": credential},
    )
    assert response.status_code == 200, response.text
    return key, response.json(), invitation


def test_header_identity_is_rejected_and_tokens_are_not_stored_raw(browser, identity_app):
    assert (
        browser.get(
            "/v1/capabilities",
            headers={
                "X-Actor-Id": str(DEV_ACTOR_ID),
                "X-Household-Id": str(DEV_HOUSEHOLD_ID),
                "X-Scopes": "system:read",
            },
        ).status_code
        == 401
    )
    headers = login(browser)
    token = browser.cookies["jarvis_session"]
    assert identity_app.store.get_session(token) is None
    assert identity_app.store.get_session(token_hash(token)) is not None
    assert browser.get("/auth/session", headers={"X-Actor-Id": str(uuid4())}).json()[
        "actor_id"
    ] == str(DEV_ACTOR_ID)
    assert headers["X-CSRF-Token"] != token
    assert token not in str(identity_app.store.audit_events())


@pytest.mark.parametrize(
    "origin,csrf",
    [(None, "valid"), ("https://evil.example", "valid"), (ORIGIN, "wrong"), (ORIGIN, "")],
)
def test_mutating_requests_require_origin_and_session_csrf(browser, origin, csrf):
    headers = login(browser)
    if origin is None:
        headers.pop("Origin")
    else:
        headers["Origin"] = origin
    if csrf != "valid":
        headers["X-CSRF-Token"] = csrf
    assert (
        browser.post(
            "/v1/jobs",
            headers=headers,
            json={"kind": "test.job", "input": {}, "idempotency_key": "csrf-test-job"},
        ).status_code
        == 403
    )
    assert browser.post("/auth/logout", headers=headers).status_code == 403


def test_login_rotation_logout_and_revocation(browser, identity_app):
    headers = login(browser)
    old = browser.cookies["jarvis_session"]
    headers = login(browser)
    with pytest.raises(AuthenticationError):
        identity_app.identity.resolve(old)
    token = browser.cookies["jarvis_session"]
    assert browser.post("/auth/logout", headers=headers).status_code == 204
    assert browser.get("/auth/session").status_code == 401
    with pytest.raises(AuthenticationError):
        identity_app.identity.resolve(token)
    login(browser)
    identity_app.store.revoke_sessions(DEV_ACTOR_ID)
    assert browser.get("/auth/session").status_code == 401


def test_scope_changes_and_household_selection_do_not_trust_headers(browser, identity_app):
    headers = login(browser)
    other = uuid4()
    assert (
        browser.post(
            "/auth/household", headers=headers, json={"household_id": str(other)}
        ).status_code
        == 403
    )
    identity_app.store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, household_id=other, role="guest")
    )
    old = browser.cookies["jarvis_session"]
    before = identity_app.identity.resolve(old)[0]
    response = browser.post("/auth/household", headers=headers, json={"household_id": str(other)})
    assert response.status_code == 200
    assert response.json()["scopes"] == ["system:read"]
    assert response.json()["expires_at"] == before.expires_at.isoformat()
    with pytest.raises(AuthenticationError):
        identity_app.identity.resolve(old)
    identity_app.store.put_membership(
        Membership(actor_id=DEV_ACTOR_ID, household_id=other, role="member")
    )
    assert "jobs:write" in browser.get("/auth/session").json()["scopes"]


def test_expired_and_disabled_development_sessions_fail(browser, identity_app):
    login(browser)
    token = browser.cookies["jarvis_session"]
    session = identity_app.identity.resolve(token)[0]
    disabled = IdentityService(identity_app.store, Settings(environment="test"))
    with pytest.raises(AuthenticationError):
        disabled.resolve(token)
    identity_app.store.delete_session(session.token_hash)
    identity_app.store.save_session(
        session.model_copy(
            update={
                "created_at": utc_now() - timedelta(days=2),
                "expires_at": utc_now() - timedelta(days=1),
            }
        )
    )
    assert browser.get("/auth/session").status_code == 401


def test_real_passkey_registration_and_login(browser, identity_app):
    key, signed_in, invitation = register(browser, identity_app)
    assert signed_in["method"] == "passkey"
    assert (
        browser.post(
            "/auth/passkeys/register/options",
            headers={"Origin": ORIGIN},
            json={"token": invitation},
        ).status_code
        == 401
    )
    assert (
        browser.post(
            "/auth/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": signed_in["csrf_token"]}
        ).status_code
        == 204
    )
    start = browser.post("/auth/passkeys/login/options", headers={"Origin": ORIGIN}).json()
    credential = key.response(start["options"])
    body = {"ceremony_id": start["ceremony_id"], "credential": credential}
    response = browser.post("/auth/passkeys/login/verify", headers={"Origin": ORIGIN}, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["method"] == "passkey"
    assert identity_app.store.get_passkey(credential["id"]).sign_count == 1
    assert (
        browser.post(
            "/auth/passkeys/login/verify", headers={"Origin": ORIGIN}, json=body
        ).status_code
        == 401
    )
    secret = identity_app.identity.enroll(DEV_ACTOR_ID, DEV_HOUSEHOLD_ID)
    options = browser.post(
        "/auth/passkeys/register/options", headers={"Origin": ORIGIN}, json={"token": secret}
    ).json()["options"]
    assert options["excludeCredentials"][0]["id"] == credential["id"]


@pytest.mark.parametrize(
    "mutation", ["origin", "challenge", "uv", "signature", "handle", "counter", "cross_origin"]
)
def test_passkey_assertions_reject_invalid_proof(browser, identity_app, mutation):
    key, _, _ = register(browser, identity_app)
    start = browser.post("/auth/passkeys/login/options", headers={"Origin": ORIGIN}).json()
    good = key.response(start["options"], counter=2)
    assert (
        browser.post(
            "/auth/passkeys/login/verify",
            headers={"Origin": ORIGIN},
            json={"ceremony_id": start["ceremony_id"], "credential": good},
        ).status_code
        == 200
    )
    start = browser.post("/auth/passkeys/login/options", headers={"Origin": ORIGIN}).json()
    changes = {
        "origin": {"origin": "https://evil.example"},
        "cross_origin": {"cross_origin": True},
        "challenge": {"challenge": "wrong"},
        "uv": {"uv": False},
        "handle": {"user_handle": uuid4().bytes},
        "counter": {"counter": 1},
    }.get(mutation, {"counter": 3})
    bad = key.response(start["options"], **changes)
    if mutation == "signature":
        bad["response"]["signature"] = "AAAA"
    body = {"ceremony_id": start["ceremony_id"], "credential": bad}
    response = browser.post("/auth/passkeys/login/verify", headers={"Origin": ORIGIN}, json=body)
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "passkey verification failed"
    assert (
        browser.post(
            "/auth/passkeys/login/verify", headers={"Origin": ORIGIN}, json=body
        ).status_code
        == 401
    )


def test_ceremonies_are_browser_bound_and_consumed_atomically(browser, identity_app):
    key, _, _ = register(browser, identity_app)
    start = browser.post("/auth/passkeys/login/options", headers={"Origin": ORIGIN}).json()
    credential = key.response(start["options"])
    with TestClient(create_app(identity_app), base_url=ORIGIN) as other:
        assert (
            other.post(
                "/auth/passkeys/login/verify",
                headers={"Origin": ORIGIN},
                json={"ceremony_id": start["ceremony_id"], "credential": credential},
            ).status_code
            == 401
        )
    binding = browser.cookies["jarvis_ceremony"]

    def verify(_):
        try:
            identity_app.identity.authenticate(start["ceremony_id"], binding, credential, None)
            return True
        except AuthenticationError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(verify, range(2))) == 1


def test_invalid_registration_burns_challenge_but_preserves_enrollment(browser, identity_app):
    secret = identity_app.identity.enroll(DEV_ACTOR_ID, DEV_HOUSEHOLD_ID)
    start = browser.post(
        "/auth/passkeys/register/options", headers={"Origin": ORIGIN}, json={"token": secret}
    ).json()
    key = SoftwarePasskey()
    bad = key.response(start["options"], registration=True, uv=False)
    body = {"ceremony_id": start["ceremony_id"], "credential": bad}
    assert (
        browser.post(
            "/auth/passkeys/register/verify", headers={"Origin": ORIGIN}, json=body
        ).status_code
        == 401
    )
    assert identity_app.store.get_enrollment(token_hash(secret)) is not None
    assert (
        browser.post(
            "/auth/passkeys/register/verify", headers={"Origin": ORIGIN}, json=body
        ).status_code
        == 401
    )


def test_passkey_revocation_prevents_new_logins(browser, identity_app):
    key, _, _ = register(browser, identity_app)
    credential = identity_app.store.passkeys(DEV_ACTOR_ID)[0]
    identity_app.store.delete_passkey(credential.credential_id)
    identity_app.store.revoke_sessions(DEV_ACTOR_ID)
    assert browser.get("/auth/session").status_code == 401
    start = browser.post("/auth/passkeys/login/options", headers={"Origin": ORIGIN}).json()
    assert (
        browser.post(
            "/auth/passkeys/login/verify",
            headers={"Origin": ORIGIN},
            json={
                "ceremony_id": start["ceremony_id"],
                "credential": key.response(start["options"]),
            },
        ).status_code
        == 401
    )


def test_enrollment_and_challenge_expiry(browser, identity_app):
    invitation = identity_app.identity.enroll(DEV_ACTOR_ID, DEV_HOUSEHOLD_ID)
    enrollment = identity_app.store.get_enrollment(token_hash(invitation))
    identity_app.store.delete_enrollment(enrollment.token_hash)
    identity_app.store.save_enrollment(
        enrollment.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    )
    assert (
        browser.post(
            "/auth/passkeys/register/options",
            headers={"Origin": ORIGIN},
            json={"token": invitation},
        ).status_code
        == 401
    )
    start = browser.post("/auth/passkeys/login/options", headers={"Origin": ORIGIN}).json()
    binding = browser.cookies["jarvis_ceremony"]
    challenge = identity_app.store.take_challenge(
        token_hash(start["ceremony_id"]), token_hash(binding)
    )
    identity_app.store.save_challenge(
        challenge.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    )
    assert (
        browser.post(
            "/auth/passkeys/login/verify",
            headers={"Origin": ORIGIN},
            json={"ceremony_id": start["ceremony_id"], "credential": {}},
        ).status_code
        == 401
    )


def test_duplicate_passkey_does_not_overwrite_owner(browser, identity_app):
    key, _, _ = register(browser, identity_app)
    invitation = identity_app.identity.enroll(DEV_ACTOR_ID, DEV_HOUSEHOLD_ID)
    start = browser.post(
        "/auth/passkeys/register/options", headers={"Origin": ORIGIN}, json={"token": invitation}
    ).json()
    response = browser.post(
        "/auth/passkeys/register/verify",
        headers={"Origin": ORIGIN},
        json={
            "ceremony_id": start["ceremony_id"],
            "credential": key.response(start["options"], registration=True),
        },
    )
    assert response.status_code == 401
    assert len(identity_app.store.passkeys(DEV_ACTOR_ID)) == 1


def test_session_creation_is_atomic_with_audit(browser, identity_app, monkeypatch):
    login(browser)
    old = browser.cookies["jarvis_session"]

    def fail(_event):
        raise RuntimeError("audit offline")

    with monkeypatch.context() as patch:
        patch.setattr(identity_app.store, "append_audit", fail)
        with pytest.raises(RuntimeError, match="audit offline"):
            identity_app.identity.development_login(SECRET, old)
    assert identity_app.identity.resolve(old)[0].token_hash == token_hash(old)


def test_removed_membership_blocks_session_and_passkey_login(browser, identity_app):
    key, _, _ = register(browser, identity_app)
    identity_app.store.delete_membership(DEV_ACTOR_ID, DEV_HOUSEHOLD_ID)
    assert browser.get("/auth/session").status_code == 403
    start = browser.post("/auth/passkeys/login/options", headers={"Origin": ORIGIN}).json()
    assert (
        browser.post(
            "/auth/passkeys/login/verify",
            headers={"Origin": ORIGIN},
            json={
                "ceremony_id": start["ceremony_id"],
                "credential": key.response(start["options"]),
            },
        ).status_code
        == 403
    )
