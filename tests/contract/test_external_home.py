import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from simon.adapters.home_client import HomeClient
from simon.config import Settings
from simon.domain.conversations import CreateThread, SubmitRun
from simon.domain.errors import AuthorizationError
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_connected import connected_setup
from tests.contract.test_model_runs import FakeModel


def test_model_dispatches_home_as_an_external_tool(store, monkeypatch):
    connected, actor, _ = connected_setup(store)
    connected.home = HomeClient(
        Settings(
            _env_file=None,
            home_api_url="http://localhost:8001",
            home_api_token=SecretStr("synthetic-token"),
        )
    )
    calls = []
    original = httpx.Client

    def respond(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        return httpx.Response(
            200,
            json=[]
            if request.url.path.endswith("commands")
            else {
                "commands": [],
                "requires_confirmation": False,
            },
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(respond))
    )
    model = FakeModel()

    def generate(request, on_delta, execute):
        assert "home_control" in request.tools
        result = json.loads(execute("home_control", '{"all_lights":true,"on":false}'))
        assert result["commands"] == []
        return model.generate(request)

    model.generate_with_tools = generate
    service = ModelConversationService(store, connected.audit, model, connected.settings, connected)
    thread = service.create(actor, CreateThread(title="Home tools", idempotency_key=str(uuid4())))
    run = service.submit(
        actor, thread.id, SubmitRun(text="Turn the lights off", idempotency_key=str(uuid4()))
    )
    writes = [body for path, body in calls if path.endswith("execute")]
    assert len(writes) == 1 and writes[0]["run_id"] == str(run.id)
    assert writes[0]["thread_id"] == str(thread.id)


def test_no_home_dispatch_without_an_active_assistant_run(store, monkeypatch):
    connected, actor, _ = connected_setup(store)
    connected.home = HomeClient(
        Settings(
            _env_file=None,
            home_api_url="http://localhost:8001",
            home_api_token=SecretStr("synthetic-token"),
        )
    )
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("unexpected dispatch"))
    execute = connected.executor(actor, uuid4(), [], lambda: actor)
    assert (
        "no longer active"
        in json.loads(execute("home_control", '{"all_lights":true,"on":false}'))["error"]
    )
    with pytest.raises(AuthorizationError):
        connected.executor(
            actor, uuid4(), [], lambda: actor.model_copy(update={"workspace_id": uuid4()})
        )("home_list_devices", "{}")
