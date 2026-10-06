from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.adapters.external_action_providers import provider_fingerprint
from simon.config import Settings
from simon.domain.context import ExplicitMemory
from simon.domain.external_actions import ExternalProviderDefinition
from simon.domain.identity import Membership
from simon.domain.models import ActorContext, Channel
from simon.domain.project_boards import BoardConnection
from simon.services.canonical import digest


def test_legacy_identity_reads_but_serializes_only_workspace_names():
    workspace, user = uuid4(), uuid4()
    actor = ActorContext.model_validate(
        {"actor_id": user, "household_id": workspace, "channel": "api"}
    )
    assert actor.workspace_id == workspace
    assert "household_id" not in actor.model_dump()
    membership = Membership.model_validate(
        {
            "actor_id": user,
            "household_id": workspace,
            "household_name": "Saved name",
            "role": "owner",
        }
    )
    assert membership.workspace_name == "Saved name"
    with pytest.raises(ValidationError):
        ActorContext(
            actor_id=user, workspace_id=workspace, household_id=uuid4(), channel=Channel.API
        )


def test_legacy_clickup_connection_keeps_both_workspace_boundaries():
    workspace, user = uuid4(), uuid4()
    connection = BoardConnection.model_validate(
        {
            "id": "clickup-test",
            "name": "ClickUp",
            "household_id": workspace,
            "actor_ids": [user],
            "credential_env": "TEST_CLICKUP_TOKEN",
            "workspace_id": "123",
            "discover_lists": True,
        }
    )
    assert connection.workspace_id == workspace
    assert connection.clickup_workspace_id == "123"
    assert BoardConnection.model_validate(connection.model_dump()) == connection


def test_old_environment_setting_remains_readable_and_new_name_takes_precedence(monkeypatch):
    old, new = uuid4(), uuid4()
    monkeypatch.setenv("SIMON_ACCOUNT_HOUSEHOLD_ID", str(old))
    assert Settings(_env_file=None).account_workspace_id == old
    monkeypatch.setenv("SIMON_ACCOUNT_WORKSPACE_ID", str(new))
    assert Settings(_env_file=None).account_workspace_id == new


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_admin_actor_id", "11111111-1111-4111-8111-111111111111"),
        ("account_workspace_id", "22222222-2222-4222-8222-222222222222"),
    ],
)
def test_production_rejects_development_identity(field, value):
    settings = {"account_admin_actor_id": uuid4(), field: value}
    with pytest.raises(ValidationError, match="development placeholders"):
        Settings(
            _env_file=None,
            environment="production",
            storage_backend="postgres",
            public_origin="https://simon.example",
            rp_id="simon.example",
            **settings,
        )


def test_existing_memory_snapshot_normalizes_workspace_scope():
    memory = ExplicitMemory.model_validate(
        {
            "household_id": uuid4(),
            "created_by": uuid4(),
            "subject": "Preference",
            "content": "Saved fact",
            "scope": "household",
        }
    )
    assert memory.scope == "workspace"


def test_external_provider_hash_keeps_historical_review_format():
    definition = ExternalProviderDefinition(
        id="gateway-test",
        name="Gateway",
        kind="gateway",
        workspace_id=uuid4(),
        actor_ids=frozenset({uuid4()}),
        credential_env="TEST_GATEWAY_TOKEN",
    )
    legacy = definition.model_dump(mode="json", exclude={"enabled"})
    legacy["household_id"] = legacy.pop("workspace_id")
    legacy["actor_ids"] = sorted(legacy["actor_ids"])
    legacy["call_recipients"] = sorted(legacy["call_recipients"])
    assert provider_fingerprint(definition) == digest(legacy)
