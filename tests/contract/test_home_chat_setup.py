import json
from datetime import timedelta
from uuid import uuid4

import pytest

from simon.adapters.model_tools import definitions
from simon.domain.conversations import CreateThread, SubmitRun
from simon.domain.errors import AuthorizationError, NotFoundError
from simon.domain.home import HomeRename
from simon.domain.models import utc_now
from simon.services.connected import ConnectedService
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_home_control import pending
from tests.contract.test_home_discovery import discovery as discovery_fixture
from tests.contract.test_home_discovery import expire
from tests.contract.test_home_tools import home_setup as home_fixture
from tests.contract.test_model_runs import FakeModel
from tests.contract.test_shelly_backend import shelly as shelly_fixture


@pytest.fixture
def shelly(store):
    return shelly_fixture.__wrapped__(store)


@pytest.fixture
def discovery(store):
    return discovery_fixture.__wrapped__(store)


def test_chat_setup_then_control_and_reconsideration(shelly):
    service, actor, _, device, _ = shelly
    calls = []
    service.home.shelly.set = lambda d, c: calls.append((d.name, c.on))
    model = FakeModel()

    def generate(request, delta, execute):
        assert {"home_rename_device", "home_setup_outlet"} <= set(request.tools)
        inventory = json.loads(execute("home_list_devices", "{}"))
        assert inventory[0]["identifier_suffix"] == "6789ab"
        renamed = json.loads(
            execute(
                "home_rename_device",
                json.dumps(
                    {
                        "device_id": device.id,
                        "name": "Bedroom lamp",
                    }
                ),
            )
        )
        assert renamed["name"] == "Bedroom lamp" and not renamed["device_commands_sent"]
        assert not service.home.device(actor, device.id).control_enabled
        args = json.dumps(
            {
                "device_id": device.id,
                "name": "Bedroom lamp",
                "room": "Bedroom",
                "load_type": "lighting",
                "control_enabled": True,
            }
        )
        result = json.loads(execute("home_setup_outlet", args))
        assert result["control_enabled"] and not result["device_commands_sent"]
        assert json.loads(execute("home_setup_outlet", args)) == result
        assert calls == []
        controlled = json.loads(execute("home_control", '{"room":"Bedroom","on":false}'))
        assert controlled["requires_confirmation"] is False
        assert controlled["commands"][0]["verified"]
        assert controlled["commands"][0]["device_name"] == "Bedroom lamp"
        return model.generate(request)

    model.generate_with_tools = generate
    conversations = ModelConversationService(
        service.store, service.audit, model, service.settings, service
    )
    thread = conversations.create(actor, CreateThread(title="Outlet", idempotency_key=str(uuid4())))
    submit = SubmitRun(
        text="Name plug ending 6789ab Bedroom lamp. It powers a lamp in the bedroom; "
        "enable control and turn it off.",
        idempotency_key=str(uuid4()),
    )
    run = conversations.submit(actor, thread.id, submit)
    assert not run.action_ids
    assert conversations.submit(actor, thread.id, submit).id == run.id
    assert calls == [("Bedroom lamp", False)]
    events = [e for e in service.store.audit_events() if e.event_type == "home.outlet_configured"]
    assert len(events) == 1

    def deeper(request, delta, execute):
        assert not {"home_control", "home_rename_device", "home_setup_outlet"} & set(request.tools)
        denied = json.loads(
            execute(
                "home_rename_device",
                json.dumps(
                    {
                        "device_id": device.id,
                        "name": "Do not save",
                    }
                ),
            )
        )
        assert "error" in denied
        return model.generate(request)

    model.generate_with_tools = deeper
    conversations.submit(
        actor,
        thread.id,
        SubmitRun(
            text=submit.text,
            parent_run_id=run.id,
            profile="deep",
            idempotency_key=str(uuid4()),
        ),
    )
    assert calls == [("Bedroom lamp", False)]
    assert service.home.device(actor, device.id).name == "Bedroom lamp"


def test_rename_preserves_cloud_names_through_refresh_and_restart(discovery):
    service, actor = discovery
    service.home.catalog.sync(actor)
    ids = [d["id"] for d in service.home.inventory(actor)]
    executor = service.executor(actor, pending(service, actor).run.id, [], lambda: actor)
    for index, device_id in enumerate(ids):
        result = json.loads(
            executor(
                "home_rename_device",
                json.dumps(
                    {
                        "device_id": device_id,
                        "name": f"My fixture {index}",
                    }
                ),
            )
        )
        assert not result["device_commands_sent"]
    expire(service, actor)
    service.home.catalog.sync(actor)
    restarted = ConnectedService(service.store, service.audit, service.settings, service.identity)
    for index, device_id in enumerate(ids):
        assert restarted.home.device(actor, device_id).name == f"My fixture {index}"
    assert not next(d for d in restarted.home.inventory(actor) if d["load_type"] == "unclassified")[
        "control_enabled"
    ]


def test_shelly_name_survives_refresh_and_replayed_edit(shelly):
    service, actor, _, device, _ = shelly
    service.home.shelly.set = lambda *args: pytest.fail("naming must not switch power")
    executor = service.executor(actor, pending(service, actor).run.id, [], lambda: actor)
    first = json.dumps({"device_id": device.id, "name": "Bedside"})
    result = executor("home_rename_device", first)
    executor("home_rename_device", json.dumps({"device_id": device.id, "name": "Reading lamp"}))
    assert json.loads(executor("home_rename_device", first)) == json.loads(result)
    sync = service.store.home_sync(actor.household_id, "shelly")
    service.store.save_home_sync(
        sync.model_copy(
            update={
                "attempted_at": utc_now() - timedelta(minutes=10),
            }
        )
    )
    service.home.catalog.sync(actor)
    restarted = ConnectedService(service.store, service.audit, service.settings, service.identity)
    saved = restarted.home.device(actor, device.id)
    assert saved.name == "Reading lamp" and saved.name_override
    assert not saved.control_enabled and saved.load_type == "unclassified"
    assert restarted.home.inventory(actor)[0]["identifier_suffix"] == "6789ab"
    assert len([e for e in service.store.audit_events() if e.event_type == "home.renamed"]) == 2


def test_configured_device_name_persists(store, tmp_path):
    service, actor, _ = home_fixture.__wrapped__(store, tmp_path)
    device = service.home.devices[0]
    service.home.catalog.rename(
        actor, HomeRename(device_id=device.id, name="Desk Beam"), service.home.devices
    )
    restarted = ConnectedService(store, service.audit, service.settings, service.identity)
    assert restarted.home.device(actor, device.id).name == "Desk Beam"


def test_chat_setup_scope_validation_and_inactive_requests(shelly):
    service, actor, _, device, _ = shelly
    attempt = pending(service, actor)
    executor = service.executor(actor, attempt.run.id, [], lambda: actor)
    name = json.dumps({"device_id": device.id, "name": "Lamp"})
    for value in ("", "  ", " leading", "x" * 101):
        assert "error" in json.loads(
            executor(
                "home_rename_device",
                json.dumps(
                    {
                        "device_id": device.id,
                        "name": value,
                    }
                ),
            )
        )
    assert "error" in json.loads(
        executor("home_rename_device", '{"device_id":"missing","name":"X"}')
    )
    unknown = {
        "device_id": device.id,
        "name": "Lamp",
        "room": "Office",
        "load_type": "unclassified",
        "control_enabled": True,
    }
    assert "error" in json.loads(executor("home_setup_outlet", json.dumps(unknown)))
    assert not service.home.device(actor, device.id).control_enabled
    member = actor.model_copy(update={"scopes": actor.scopes - {"identity:manage"}})
    assert "home_rename_device" in service.available(member)
    assert "home_setup_outlet" not in service.available(member)
    with pytest.raises(AuthorizationError):
        service.executor(member, attempt.run.id, [], lambda: member)("home_setup_outlet", "{}")
    readonly = actor.model_copy(update={"scopes": frozenset({"home:read", "threads:write"})})
    assert "home_rename_device" not in service.available(readonly)
    revalidations = iter((actor, readonly))
    assert "error" in json.loads(
        service.executor(actor, attempt.run.id, [], lambda: next(revalidations))(
            "home_rename_device", name
        )
    )
    with pytest.raises(NotFoundError):
        service.home.catalog.rename(
            actor.model_copy(update={"household_id": uuid4()}),
            HomeRename(device_id=device.id, name="No"),
            (),
        )
    for update in ({"status": "failed"}, {"expires_at": utc_now() - timedelta(seconds=1)}):
        service.store.save_attempt(attempt.model_copy(update=update))
        assert "error" in json.loads(executor("home_rename_device", name))
        assert "error" in json.loads(
            executor(
                "home_setup_outlet",
                json.dumps(
                    {
                        **unknown,
                        "load_type": "lighting",
                    }
                ),
            )
        )
    assert service.home.device(actor, device.id).name == device.name


def test_new_tool_schemas_are_strict():
    for tool in definitions(("home_rename_device", "home_setup_outlet")):
        assert tool["strict"] is True
        schema = tool["parameters"]
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False
