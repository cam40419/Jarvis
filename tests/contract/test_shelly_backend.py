from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from ipaddress import IPv4Address
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as SchemaError

from simon.adapters.google import ConnectedError
from simon.api.app import AppContainer, create_app
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.home import DiscoveredDevice, HomeStatus, OutletPower, OutletSetup, PowerSample
from simon.domain.models import utc_now
from simon.services.connected import ConnectedService
from simon.services.power import PowerMonitor
from tests.contract.test_connected import connected_setup


@pytest.fixture
def shelly(store):
    service, actor, token = connected_setup(store)
    settings = service.settings.model_copy(
        update={
            "shelly_lan_discovery": True,
            "home_household_id": actor.household_id,
            "power_poll_seconds": 60,
        }
    )
    service = ConnectedService(store, service.audit, settings, service.identity)
    found = DiscoveredDevice(
        remote_id="shellyplugusg4-0123456789ab",
        name="Shelly outlet",
        address=IPv4Address("192.168.1.45"),
    )
    service.home.shelly.discover = lambda: (found,)
    service.home.catalog.sync(actor, force=True)
    device = service.home.catalog.devices(actor.household_id, ())[0]
    state = HomeStatus(
        device_id=device.id,
        online=True,
        on=False,
        watts=5,
        energy_wh=10,
        capabilities=("power",),
        uptime_seconds=120,
    )
    service.home.shelly.read = lambda d: state
    service.home.shelly.meter = lambda d: state
    return service, actor, token, device, found


def test_discover_setup_control_and_receipt_replay(shelly):
    service, actor, _, device, _ = shelly
    home = service.home
    assert not home.inventory(actor)[0]["control_enabled"]
    calls = []
    home.shelly.set = lambda d, c: calls.append(c)
    power = OutletPower(on=False, idempotency_key="switch-request-1")
    with pytest.raises(AuthorizationError):
        home.outlet_power(actor, device.id, power, lambda: actor)
    setup = OutletSetup(
        name="Bedroom lamp", room="Bedroom", load_type="lighting", control_enabled=True
    )
    home.setup_outlet(actor, device.id, setup)
    first = home.outlet_power(actor, device.id, power, lambda: actor)
    assert first.status == "succeeded" and first.verified and first.run_id is None
    assert home.outlet_power(actor, device.id, power, lambda: actor) == first and len(calls) == 1
    with pytest.raises(ConnectedError):
        home.outlet_power(actor, device.id, power.model_copy(update={"on": True}), lambda: actor)
    assert home.store.home_command(actor.household_id, actor.actor_id, first.id) == first
    assert home.store.home_command(uuid4(), actor.actor_id, first.id) is None
    with pytest.raises(SchemaError):
        OutletSetup(name="Unknown", load_type="unclassified", control_enabled=True)


def test_discovery_address_updates_preserve_setup_and_missed_broadcasts(shelly):
    service, actor, _, device, found = shelly
    home = service.home
    home.setup_outlet(
        actor,
        device.id,
        OutletSetup(name="Purifier", room="Office", load_type="air_purifier", control_enabled=True),
    )

    def refresh(items):
        sync = service.store.home_sync(actor.household_id, "shelly")
        service.store.save_home_sync(
            sync.model_copy(update={"attempted_at": utc_now() - timedelta(minutes=10)})
        )
        home.shelly.discover = lambda: items
        home.catalog.sync(actor, force=True)

    refresh((found.model_copy(update={"address": IPv4Address("192.168.1.99")}),))
    changed = home.device(actor, device.id)
    assert changed.address == IPv4Address("192.168.1.99") and changed.name == "Purifier"
    assert changed.control_enabled and changed.load_type == "air_purifier"
    refresh(())
    assert home.device(actor, device.id).present
    home.catalog.settings = home.catalog.settings.model_copy(update={"shelly_lan_discovery": False})
    assert home.device(actor, device.id).control_enabled
    home.devices = (device.model_copy(update={"id": "legacy-plug", "source": "configured"}),)
    inventory = home.inventory(actor)
    assert len(inventory) == 1 and inventory[0]["id"] == "legacy-plug"


def test_poll_is_read_only_claimed_once_and_reports_failure(shelly):
    service, actor, _, _device, _ = shelly
    monitor = PowerMonitor(service.home)
    entered, release = Event(), Event()
    calls = []

    def meter(d):
        calls.append(1)
        entered.set()
        assert release.wait(10)
        raise ConnectedError("Outlet is unavailable")

    service.home.shelly.meter = meter
    service.home.shelly.set = lambda *a: pytest.fail("metering must never switch")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(monitor.poll, actor)
        assert entered.wait(10)
        try:
            assert pool.submit(monitor.poll, actor).result(timeout=5)[0].state == "pending"
        finally:
            release.set()
        assert first.result(timeout=5)[0].state == "unavailable"
    assert len(calls) == 1 and monitor.overview(actor)["devices"][0]["stale"]
    assert monitor.overview(actor.model_copy(update={"household_id": uuid4()}))["devices"] == []


def test_meter_history_consumption_resets_gaps_and_retention(shelly):
    service, actor, _, device, _ = shelly
    monitor = PowerMonitor(service.home)
    start = utc_now() - timedelta(minutes=20)
    # A reboot can reset a counter and still report a larger value by the next poll.
    rows = [(0, 100, 1000), (60, 102, 1060), (120, 103, 10), (180, 104, 70), (600, 110, 490)]
    for seconds, energy, uptime in rows:
        service.store.save_power_sample(
            PowerSample(
                household_id=actor.household_id,
                device_id=device.id,
                captured_at=start + timedelta(seconds=seconds),
                state="ok",
                reading=HomeStatus(
                    device_id=device.id, watts=120, energy_wh=energy, uptime_seconds=uptime
                ),
            )
        )
    result = monitor.history(actor, device.id, start, start + timedelta(minutes=15))
    assert result["summary"]["observed_energy_wh"] == 3
    assert result["summary"]["observed_energy_kwh"] == 0.003
    assert result["summary"]["counter_resets"] == 1 and result["summary"]["gaps"] == 1
    assert result["summary"]["covered_seconds"] == 120
    with pytest.raises(ValidationError):
        monitor.history(actor, device.id, start, start + timedelta(days=2))
    with pytest.raises(NotFoundError):
        monitor.history(
            actor.model_copy(update={"household_id": uuid4()}), device.id, start, utc_now()
        )
    old = PowerSample(
        household_id=actor.household_id,
        device_id=device.id,
        captured_at=start - timedelta(days=100),
    )
    service.store.save_power_sample(old)
    monitor.poll(actor)
    assert not service.store.power_samples(
        actor.household_id, device.id, old.captured_at, old.captured_at + timedelta(seconds=1)
    )


def test_backend_endpoints_require_auth_csrf_and_owner_setup(shelly):
    service, actor, token, device, _ = shelly
    container = AppContainer(store=service.store, settings=service.settings)
    container.connected.home = service.home
    container.power = PowerMonitor(service.home)
    calls = []
    service.home.shelly.set = lambda d, c: calls.append(c)
    with TestClient(create_app(container), base_url="http://localhost:8000") as client:
        assert client.get("/v1/home/power").status_code == 401
        client.cookies.set("simon_session", token)
        assert client.get("/v1/home/outlets").json()[0]["address"] == "192.168.1.45"
        session = client.get("/auth/session").json()
        headers = {"Origin": "http://localhost:8000", "X-CSRF-Token": session["csrf_token"]}
        setup = {
            "name": "Lamp",
            "room": "Bedroom",
            "load_type": "lighting",
            "control_enabled": True,
        }
        path = f"/v1/home/outlets/{device.id}"
        assert client.post(path + "/setup", json=setup).status_code == 403
        assert client.post(path + "/setup", json=setup, headers=headers).status_code == 200
        command = {"on": False, "idempotency_key": "backend-outlet-test"}
        assert client.post(path + "/power", json=command).status_code == 403
        assert client.post(path + "/power", json=command, headers=headers).json()["verified"]
        assert client.post(path + "/power", json=command, headers=headers).status_code == 200
        assert len(calls) == 1
        assert client.post("/v1/home/power/refresh", json={}).status_code == 403
        assert client.post("/v1/home/power/refresh", json={}, headers=headers).status_code == 200
        assert client.get("/v1/home/power").json()["devices"][0]["latest"]["reading"]["watts"] == 5
        assert client.get(f"/v1/home/devices/{device.id}/power").status_code == 200
        assert (
            client.get(f"/v1/home/devices/{device.id}/power?start=2026-01-01T00:00:00").status_code
            == 422
        )
    member = actor.model_copy(update={"scopes": actor.scopes - {"identity:manage"}})
    with pytest.raises(AuthorizationError):
        service.home.setup_outlet(member, device.id, OutletSetup.model_validate(setup))


def test_background_metering_starts_and_stops_with_application(shelly):
    service, actor, _, _, _ = shelly
    settings = service.settings.model_copy(update={"power_monitoring_enabled": True})
    container = AppContainer(store=service.store, settings=settings)
    container.connected.home = service.home
    container.power = PowerMonitor(service.home)
    completed = Event()
    calls = []

    def poll(current):
        assert current.household_id == actor.household_id and current.scopes == {"home:read"}
        calls.append(1)
        completed.set()
        return ()

    container.power.poll = poll
    with TestClient(create_app(container)):
        assert completed.wait(5)
    assert calls == [1]


def test_unknown_write_result_is_not_retried_after_new_service(shelly):
    service, actor, _, device, _ = shelly
    home = service.home
    home.setup_outlet(
        actor, device.id, OutletSetup(name="Lamp", load_type="lighting", control_enabled=True)
    )
    calls = []

    def fail(*args):
        calls.append(1)
        raise ConnectedError("Timeout", unknown=True)

    home.shelly.set = fail
    request = OutletPower(on=True, idempotency_key="unknown-switch-retry")
    assert home.outlet_power(actor, device.id, request, lambda: actor).status == "unknown"
    fresh = ConnectedService(service.store, service.audit, service.settings, service.identity)
    fresh.home.shelly.set = lambda *a: pytest.fail("must not resend after restart")
    assert fresh.home.outlet_power(actor, device.id, request, lambda: actor).status == "unknown"
    assert len(calls) == 1
