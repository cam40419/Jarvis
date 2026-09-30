import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from simon.adapters.google import ConnectedError
from simon.adapters.home_client import HomeClient
from simon.config import Settings
from simon.domain.models import ActorContext, Channel


def setup_client(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(**kw, transport=httpx.MockTransport(handler))
    )
    actor = ActorContext(
        actor_id=uuid4(),
        household_id=uuid4(),
        channel=Channel.CHAT,
        scopes=frozenset({"home:read", "home:control"}),
    )
    return HomeClient(
        Settings(
            _env_file=None,
            home_api_url="http://localhost:8001",
            home_api_token=SecretStr("synthetic-service-token"),
        )
    ), actor


def test_external_tool_sends_identity_and_stable_run_without_device_credentials(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"commands": [], "requires_confirmation": False})

    client, actor = setup_client(monkeypatch, respond)
    run, thread = uuid4(), uuid4()
    client.execute(
        actor, "home_control", {"all_lights": True, "on": False}, run_id=run, thread_id=thread
    )
    request = calls[0]
    assert request.url.path == "/v1/tools/execute"
    assert request.headers["authorization"] == "Bearer synthetic-service-token"
    assert request.headers["x-actor-id"] == str(actor.actor_id)
    assert request.headers["x-household-id"] == str(actor.household_id)
    assert json.loads(request.content) == {
        "name": "home_control",
        "arguments": {"all_lights": True, "on": False},
        "run_id": str(run),
        "thread_id": str(thread),
    }


@pytest.mark.parametrize("failure", ["timeout", "redirect", "server", "invalid"])
def test_uncertain_writes_are_never_retried(monkeypatch, failure):
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private host details", request=request)
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://example.invalid"})
        return httpx.Response(500 if failure == "server" else 200, text="not json")

    client, actor = setup_client(monkeypatch, respond)
    with pytest.raises(ConnectedError) as caught:
        client.execute(
            actor,
            "home_control",
            {"all_lights": True, "on": False},
            run_id=uuid4(),
            thread_id=uuid4(),
        )
    assert len(calls) == 1
    assert "private host" not in str(caught.value)
    if failure != "redirect":
        assert caught.value.unknown


def test_unconfigured_home_does_not_contact_network(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("unexpected network"))
    client = HomeClient(Settings(_env_file=None))
    actor = ActorContext(actor_id=uuid4(), household_id=uuid4(), channel=Channel.CHAT)
    assert not client.configured and client.available(actor) == ()
    with pytest.raises(ConnectedError):
        client.execute(actor, "home_list_devices", {}, run_id=uuid4(), thread_id=uuid4())


def test_offline_home_does_not_break_assistant_history(monkeypatch):
    def respond(request):
        raise httpx.ConnectError("offline", request=request)

    client, actor = setup_client(monkeypatch, respond)
    assert client.commands(actor, uuid4()) == ()


def test_cross_household_receipts_are_not_exposed(monkeypatch):
    client, actor = setup_client(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json=[
                {
                    "actor_id": str(uuid4()),
                    "household_id": str(uuid4()),
                    "run_id": str(uuid4()),
                }
            ],
        ),
    )
    assert client.commands(actor, uuid4()) == ()


def test_assistant_has_no_physical_home_routes(client, auth_headers):
    for path in (
        "/home",
        "/displays",
        "/automations",
        "/v1/home/devices",
        "/v1/printers/a1/status",
        "/v1/workflows",
    ):
        assert client.get(path).status_code == 404
    assert client.get("/v1/connections/home").json() == {"configured": False, "url": None}
    assert client.get("/v1/work/overview").status_code == 200
