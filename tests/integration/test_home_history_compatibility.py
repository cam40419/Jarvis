import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from simon.adapters.model_tools import definitions
from simon.adapters.postgres import PostgresStore
from simon.api.app import AppContainer, create_app
from simon.domain.conversations import CreateThread, Message, ModelAttempt, Run
from simon.domain.errors import AuthorizationError
from simon.domain.model import ModelRequest
from simon.domain.models import utc_now
from tests.contract.test_connected import connected_setup
from tests.unit.test_home_adapters import device

pytestmark = pytest.mark.postgres


def test_retired_home_tool_history_loads_after_restart_without_reenabling_tool(postgres_url):
    store = PostgresStore(postgres_url)
    service, actor, token = connected_setup(store)
    thread = service.conversations.create(
        actor, CreateThread(title="Older home conversation", idempotency_key=str(uuid4()))
    )
    user = Message(thread_id=thread.id, sequence=1, role="user", text="A saved lighting request")
    reply = Message(thread_id=thread.id, sequence=2, role="assistant", text="An old preview.")
    # Seed the pre-upgrade format without validating it against today's tool enum.
    request = ModelRequest(
        model="historical-model", instructions="Historical prompt", input_text="{}"
    ).model_copy(update={"tools": ("home_list_devices", "propose_home_change")})
    run = Run(
        thread_id=thread.id,
        actor_id=actor.actor_id,
        context=(),
        model_request=request,
        capability_manifest=request.tools,
        input_message_id=user.id,
        output_message_id=reply.id,
    )
    saved_attempt = ModelAttempt(
        run=run,
        user=user,
        household_id=actor.household_id,
        status="succeeded",
        expires_at=utc_now(),
    )
    snapshot = run.model_dump(mode="json")
    attempt = saved_attempt.model_dump(mode="json")
    with store.transaction():
        store.insert_message(user)
        store.insert_message(reply)
        store.insert_run(run, ())
        store.save_attempt(saved_attempt)

    settings = service.settings.model_copy(
        update={"storage_backend": "postgres", "database_url": SecretStr(postgres_url)}
    )
    restarted = AppContainer(settings=settings)
    with TestClient(create_app(restarted), base_url="http://localhost:8000") as client:
        client.cookies.set("simon_session", token)
        for path in (f"/v1/threads/{thread.id}/latest-run", f"/v1/runs/{run.id}"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json() == snapshot
        answers = client.get(f"/v1/threads/{thread.id}/answers")
        assert answers.status_code == 200 and answers.json()[0]["run_id"] == str(run.id)
        assert restarted.store.attempt(run.id).model_dump(mode="json") == attempt

        # Reading old history does not reintroduce the confirmation tool into new chats.
        restarted.connected.home.devices = (
            device(household_id=actor.household_id, load_type="lighting", control_enabled=True),
        )
        available = restarted.connected.available(actor)
        assert "home_control" in available and "propose_home_change" not in available
        with pytest.raises(ValueError, match="unsupported tool"):
            definitions(("propose_home_change",))
        execute = restarted.connected.executor(actor, run.id, [], lambda: actor)
        with pytest.raises(AuthorizationError, match="tool unavailable"):
            execute("propose_home_change", '{"device_id":"office-light","on":false}')

    # Preserve historical intent verbatim; do not migrate previews into executable commands.
    with store.transaction():
        row = store.connection.execute(
            "SELECT snapshot FROM runs WHERE id = %s", (run.id,)
        ).fetchone()
        assert row["snapshot"] == snapshot
    snapshot["model_request"]["tools"] = ["unknown_tool"]
    with pytest.raises(ValidationError):
        Run.model_validate_json(json.dumps(snapshot))
