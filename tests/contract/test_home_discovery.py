from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from pydantic import SecretStr

from simon.adapters.google import ConnectedError
from simon.domain.errors import AuthorizationError, NotFoundError
from simon.domain.home import DiscoveredDevice, HomeOrganization
from simon.domain.models import utc_now
from simon.services.connected import ConnectedService
from tests.contract.test_connected import connected_setup


@pytest.fixture
def discovery(store):
    service, actor, _ = connected_setup(store)
    settings = service.settings.model_copy(
        update={
            "lifx_token": SecretStr("synthetic-lifx"),
            "tuya_client_id": "synthetic-project",
            "tuya_client_secret": SecretStr("synthetic-tuya"),
            "home_household_id": actor.household_id,
        }
    )
    service = ConnectedService(store, service.audit, settings, service.identity)
    service.home.lifx.discover = lambda: (
        DiscoveredDevice(
            remote_id="d073d5000001",
            name="Beam",
            room="Vendor room",
            groups=("Vendor group",),
            lighting=True,
        ),
    )
    service.home.tuya.discover = lambda: (
        DiscoveredDevice(remote_id="hex-1", name="Up Arrow", lighting=True),
        DiscoveredDevice(remote_id="hex-2", name="Down Arrow", lighting=True),
        DiscoveredDevice(remote_id="unknown", name="Other appliance"),
    )
    service.home.lifx.set = service.home.tuya.set = lambda *args: pytest.fail("No device commands")
    return service, actor


def expire(service, actor):
    for provider in ("lifx", "tuya"):
        record = service.store.home_sync(actor.household_id, provider)
        service.store.save_home_sync(
            record.model_copy(
                update={
                    "attempted_at": utc_now() - timedelta(minutes=6),
                    "lease_until": utc_now() - timedelta(seconds=1),
                }
            )
        )


def test_discovery_persists_and_organization_survives_refresh(discovery):
    service, actor = discovery
    assert "home_list_devices" in service.available(actor)
    assert "home_organize_devices" in service.available(actor)
    result = service.home.catalog.sync(actor)
    assert [r["count"] for r in result] == [1, 3, 0]
    devices = service.home.inventory(actor)
    assert (
        len(devices) == 4
        and not next(d for d in devices if d["name"] == "Other appliance")["control_enabled"]
    )
    assert "remote_id" not in str(devices) and "synthetic" not in str(result)
    ids = tuple(d["id"] for d in devices if d["control_enabled"])
    changed = HomeOrganization(device_ids=ids, room="Office", groups=("Desk lights", "Evening"))
    output = service.home.catalog.organize(actor, changed, (), operation_key="organize-office")
    assert not output["device_commands_sent"]
    assert (
        service.home.catalog.organize(actor, changed, (), operation_key="organize-office") == output
    )
    assert len([e for e in service.store.audit_events() if e.event_type == "home.organized"]) == 1
    expire(service, actor)
    service.home.catalog.sync(actor)
    restarted = ConnectedService(service.store, service.audit, service.settings, service.identity)
    for device in restarted.home.inventory(actor):
        if device["id"] in ids:
            assert device["room"] == "Office" and device["groups"] == ["Desk lights", "Evening"]
    assert len(restarted.home.inventory(actor)) == 4


def test_household_scope_and_account_rotation(discovery):
    service, actor = discovery
    service.home.catalog.sync(actor)
    other = actor.model_copy(update={"household_id": uuid4()})
    assert service.home.inventory(other) == []
    assert not any(p["configured"] for p in service.home.catalog.sync(other))
    device = service.home.inventory(actor)[0]
    with pytest.raises(NotFoundError):
        service.home.catalog.organize(
            other,
            HomeOrganization(device_ids=(device["id"],), room="Other"),
            (),
            operation_key="cross-household",
        )
    with pytest.raises(AuthorizationError):
        service.home.catalog.organize(
            actor.model_copy(update={"scopes": frozenset({"home:read"})}),
            HomeOrganization(device_ids=(device["id"],), room="Other"),
            (),
            operation_key="no-authority",
        )
    settings = service.settings.model_copy(
        update={"lifx_token": SecretStr("rotated"), "tuya_client_secret": None}
    )
    rotated = ConnectedService(service.store, service.audit, settings, service.identity)
    assert rotated.home.inventory(actor) == []
    rotated.home.lifx.discover = service.home.lifx.discover
    rotated.home.catalog.sync(actor)
    assert len(rotated.home.inventory(actor)) == 1


def test_provider_failure_preserves_inventory_and_missing_disables_control(discovery):
    service, actor = discovery
    service.home.catalog.sync(actor)
    before = service.home.inventory(actor)
    expire(service, actor)

    def fail():
        raise ConnectedError("Tuya data center suspended")

    service.home.tuya.discover = fail
    service.home.lifx.discover = lambda: ()
    result = service.home.catalog.sync(actor)
    assert result[0]["status"] == "ready" and result[1]["status"] == "error"
    assert result[1]["count"] == 3 and "suspended" in result[1]["error"]
    now = service.home.inventory(actor)
    assert len(now) == len(before)
    beam = next(d for d in now if d["name"] == "Beam")
    assert not beam["present"] and not beam["control_enabled"]
    with pytest.raises(AuthorizationError):
        service.home.device(actor, beam["id"], control=True)


def test_concurrent_discovery_does_not_hold_database_transaction(discovery):
    service, actor = discovery
    entered, release = Event(), Event()
    calls = []
    original = service.home.lifx.discover

    def wait():
        calls.append(1)
        entered.set()
        assert release.wait(10)
        return original()

    service.home.lifx.discover = wait
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.home.catalog.sync, actor)
        assert entered.wait(10)
        try:
            second = pool.submit(service.home.catalog.sync, actor)
            assert second.result(timeout=5)[0]["status"] == "syncing"
        finally:
            release.set()
        assert first.result(timeout=5)[0]["status"] == "ready"
    assert calls == [1]


def test_organization_is_atomic_and_rolls_back_with_audit(discovery, monkeypatch):
    service, actor = discovery
    service.home.catalog.sync(actor)
    devices = service.home.inventory(actor)
    with pytest.raises(NotFoundError):
        service.home.catalog.organize(
            actor,
            HomeOrganization(device_ids=(devices[0]["id"], "missing"), room="Wrong"),
            (),
            operation_key="missing-device",
        )
    assert service.home.inventory(actor) == devices

    def fail(**kwargs):
        raise RuntimeError("audit failed")

    monkeypatch.setattr(service.audit, "record", fail)
    with pytest.raises(RuntimeError):
        service.home.catalog.organize(
            actor,
            HomeOrganization(device_ids=(devices[0]["id"],), room="Wrong"),
            (),
            operation_key="audit-failure",
        )
    assert service.home.inventory(actor) == devices


def test_model_tool_discovers_then_organizes_with_replay(discovery):
    import json

    service, actor = discovery
    executor = service.executor(actor, uuid4(), [], lambda: actor)
    devices = json.loads(executor("home_list_devices", "{}"))
    arguments = json.dumps(
        {"device_ids": [d["id"] for d in devices], "room": "Office", "groups": None}
    )
    result = json.loads(executor("home_organize_devices", arguments))
    assert len(result["updated"]) == 4 and not result["device_commands_sent"]
    assert json.loads(executor("home_organize_devices", arguments)) == result
    assert json.loads(executor("home_refresh_devices", "{}"))["providers"][0]["status"] == "ready"
    assert "error" in json.loads(executor("home_organize_devices", '{"device_ids":[]}'))


def test_stale_sync_claim_cannot_replace_new_result(discovery):
    service, actor = discovery

    def supersede():
        record = service.store.home_sync(actor.household_id, "lifx")
        service.store.save_home_sync(
            record.model_copy(update={"claim_id": uuid4(), "status": "ready"})
        )
        return ()

    service.home.lifx.discover = supersede
    result = service.home.catalog.sync(actor)
    assert result[0]["status"] == "ready"
    assert len(service.home.inventory(actor)) == 3


def test_startup_discovers_without_user_request(discovery):
    from fastapi.testclient import TestClient

    from simon.api.app import AppContainer, create_app

    service, actor = discovery
    settings = service.settings.model_copy(update={"home_auto_discovery": True})
    container = AppContainer(store=service.store, settings=settings)
    container.connected = service
    service.home.catalog.settings = settings
    completed = Event()
    sync = service.home.catalog.sync_target

    def synced():
        sync()
        completed.set()

    service.home.catalog.sync_target = synced
    with TestClient(create_app(container)):
        assert completed.wait(5)
        assert len(service.home.inventory(actor)) == 4
