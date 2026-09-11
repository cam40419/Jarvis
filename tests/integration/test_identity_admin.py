import sys
from uuid import uuid4

import pytest
from pydantic import SecretStr

from jarvis.config import Settings
from jarvis.domain.errors import NotFoundError
from jarvis.domain.identity import Passkey
from jarvis.services.identity import IdentityService, token_hash

pytestmark = pytest.mark.postgres


def test_operator_enrollment_membership_and_revocation(postgres_url, monkeypatch, capsys):
    from jarvis import identity_admin
    from jarvis.adapters.postgres import PostgresStore

    settings = Settings(storage_backend="postgres", database_url=SecretStr(postgres_url))
    monkeypatch.setattr(identity_admin, "get_settings", lambda: settings)
    store = PostgresStore(postgres_url)
    actor_id, household_id = uuid4(), uuid4()

    def command(*arguments):
        monkeypatch.setattr(sys, "argv", ["identity_admin", *arguments])
        identity_admin.main()
        return capsys.readouterr().out

    command(
        "membership",
        "--actor-id",
        str(actor_id),
        "--household-id",
        str(household_id),
        "--role",
        "owner",
        "--display-name",
        "Test owner",
    )
    assert store.memberships(actor_id)[0].role == "owner"
    output = command("enroll", "--actor-id", str(actor_id), "--household-id", str(household_id))
    secret = output.strip().splitlines()[-1]
    assert store.get_enrollment(token_hash(secret)).actor_id == actor_id
    assert store.get_enrollment(secret) is None
    assert secret not in str(store.audit_events())
    identity = IdentityService(store, settings)
    with store.transaction():
        token, _ = identity._issue(actor_id, household_id, "passkey")
    command("revoke-sessions", "--actor-id", str(actor_id))
    assert store.get_session(token_hash(token)) is None
    store.save_passkey(
        Passkey(
            credential_id="test-credential",
            actor_id=actor_id,
            public_key="test-public-key",
            sign_count=0,
            device_type="single_device",
            backed_up=False,
        )
    )
    command("revoke-passkey", "--credential-id", "test-credential")
    assert store.get_passkey("test-credential") is None
    with pytest.raises(NotFoundError):
        command("revoke-passkey", "--credential-id", "missing")
    assert {event.event_type for event in store.audit_events()} >= {
        "identity.enrollment_issued",
        "identity.membership_updated",
        "identity.sessions_revoked",
        "identity.passkey_revoked",
    }
    command("remove-membership", "--actor-id", str(actor_id), "--household-id", str(household_id))
    assert store.memberships(actor_id) == ()
    monkeypatch.setattr(identity_admin, "get_settings", lambda: Settings())
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        command("enroll", "--actor-id", str(actor_id), "--household-id", str(household_id))
