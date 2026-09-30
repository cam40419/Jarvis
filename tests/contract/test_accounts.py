from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from simon.domain.accounts import AccountAccess, InviteAccount
from simon.domain.conversations import CreateThread
from simon.domain.errors import (
    AuthenticationError,
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.models import utc_now
from simon.services.accounts import AccountService
from simon.services.identity import token_hash
from tests.contract.test_connected import connected_setup
from tests.passkey_helper import SoftwarePasskey


def register(identity, invitation):
    key = SoftwarePasskey()
    flow, binding = identity.registration_options(invitation)
    credential = key.response(flow["options"], registration=True)
    token, session = identity.register(flow["ceremony_id"], binding, credential, None)
    return key, token, identity.actor(session)


def test_invited_account_private_workspace_and_no_admin_access(store):
    connected, owner, _ = connected_setup(store)
    service = AccountService(connected.identity)
    request = InviteAccount(display_name="Alex", idempotency_key=uuid4())
    issued = service.invite(owner, request)
    account_id = UUID(issued["account"]["actor_id"])
    assert UUID(issued["account"]["household_id"]) != owner.household_id
    secret = issued["enrollment_token"]
    assert secret and store.get_enrollment(secret) is None
    assert secret not in str(store.managed_accounts()) + str(store.audit_events())
    assert service.invite(owner, request)["enrollment_token"] is None
    assert len(service.list(owner)) == 1
    assert "enrollment_hash" not in str(service.list(owner))
    with pytest.raises(IdempotencyConflictError):
        service.invite(owner, request.model_copy(update={"display_name": "Other"}))
    _, token, invited = register(connected.identity, secret)
    assert invited.actor_id == account_id and invited.scopes
    assert len(store.memberships(account_id)) == 1
    assert service.list(owner)[0]["status"] == "active"
    with pytest.raises(AuthenticationError):
        connected.identity.registration_options(secret)
    with pytest.raises(InvalidTransitionError):
        service.renew(owner, account_id)  # Web administrator cannot mint a login for active users.
    for action in (
        lambda: service.list(invited),
        lambda: service.invite(invited, request),
        lambda: service.renew(invited, account_id),
    ):
        with pytest.raises(AuthorizationError):
            action()
    with pytest.raises(AuthorizationError):
        connected.identity.switch_household(token, owner.household_id)
    thread = connected.conversations.create(
        owner, CreateThread(title="Private", idempotency_key="owner-private-thread")
    )
    with pytest.raises(NotFoundError):
        connected.conversations.get(invited, thread.id)
    assert connected.conversations.list(invited, 0, 10) == ()


def test_invitation_renewal_expiry_and_account_revocation(store):
    connected, owner, _ = connected_setup(store)
    service = AccountService(connected.identity)
    issued = service.invite(owner, InviteAccount(display_name="Jamie", idempotency_key=uuid4()))
    identifier = UUID(issued["account"]["actor_id"])
    old = issued["enrollment_token"]
    enrollment = store.get_enrollment(token_hash(old))
    store.delete_enrollment(token_hash(old))
    store.save_enrollment(
        enrollment.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    )
    assert service.list(owner)[0]["status"] == "invitation_expired"
    with pytest.raises(AuthenticationError):
        connected.identity.registration_options(old)
    renewed = service.renew(owner, identifier)
    assert renewed["enrollment_token"] != old
    assert store.get_enrollment(token_hash(old)) is None
    key, token, invited = register(connected.identity, renewed["enrollment_token"])
    record = service.get(identifier)
    result = service.access(
        owner, identifier, AccountAccess(enabled=False, expected_version=record.version)
    )
    assert result["status"] == "disabled"
    with pytest.raises(AuthenticationError):
        connected.identity.resolve(token)
    with pytest.raises(AuthorizationError):
        connected.identity.membership(invited.actor_id, invited.household_id)
    with pytest.raises(InvalidTransitionError):
        service.access(
            owner, identifier, AccountAccess(enabled=True, expected_version=record.version)
        )
    with pytest.raises(InvalidTransitionError):
        service.renew(owner, identifier)
    result = service.access(
        owner, identifier, AccountAccess(enabled=True, expected_version=result["version"])
    )
    assert result["status"] == "active"
    assert len(store.passkeys(identifier)) == 1  # Disable preserves the user's key and data.
    flow, binding = connected.identity.authentication_options()
    _, session = connected.identity.authenticate(
        flow["ceremony_id"], binding, key.response(flow["options"], registration=False), None
    )
    assert session.actor_id == invited.actor_id
    with pytest.raises(NotFoundError):
        service.renew(owner, uuid4())
