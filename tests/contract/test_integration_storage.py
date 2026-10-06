"""Account connections survive separate service instances and transaction rollback."""

from uuid import uuid4

import pytest

from simon.domain.integrations import ConnectIntegration
from tests.unit.test_external_actions import actor
from tests.unit.test_integrations import service


def test_connections_survive_service_restart_and_disconnect(store, tmp_path):
    api, _, _ = service(tmp_path, store)
    saved = api.connect(actor(), "clickup", ConnectIntegration(credential="pk_persisted"))
    worker, _, _ = service(tmp_path, store)
    connection = worker.board_connections(actor())[0]
    assert worker.credentials[connection.credential_env] == "pk_persisted"
    assert worker.list(actor())[0]["id"] == saved["id"]
    assert worker.list(actor(actor_id=uuid4())) == []
    api.disconnect(actor(), saved["id"])
    assert worker.list(actor()) == []
    assert worker.credentials.get(connection.credential_env) is None


def test_connections_rollback_with_the_transaction(store, tmp_path):
    api, _, _ = service(tmp_path, store)
    with pytest.raises(RuntimeError), store.transaction(actor().workspace_id):
        api.connect(
            actor(),
            "twilio",
            ConnectIntegration(
                credential="phone-token", account_sid="AC" + "b" * 32, from_number="+15551234567"
            ),
        )
        raise RuntimeError("rollback")
    assert api.list(actor()) == []
