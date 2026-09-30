import json
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError

from simon.adapters.google import ConnectedError
from simon.domain.errors import AuthorizationError, NotFoundError
from simon.domain.home import HomeStatus, HomeStatusQuery
from tests.contract.test_home_tools import home_setup as home_fixture


@pytest.fixture
def home_setup(store, tmp_path):
    return home_fixture.__wrapped__(store, tmp_path)


def test_status_batch_runs_concurrently_and_keeps_partial_results(home_setup, monkeypatch):
    service, actor, _ = home_setup
    first = service.home.devices[0]
    second = first.model_copy(update={"id": "second-light"})
    service.home.devices = (first, second)
    catalog_calls = 0
    catalog_devices = service.home.catalog.devices

    def counted_devices(*args):
        nonlocal catalog_calls
        catalog_calls += 1
        return catalog_devices(*args)

    monkeypatch.setattr(service.home.catalog, "devices", counted_devices)
    barrier = Barrier(2, timeout=3)

    def read(device):
        barrier.wait()
        if device.id == second.id:
            raise ConnectedError("Device is unavailable")
        return HomeStatus(device_id=device.id, on=True, online=True)

    service.home.lifx.read = read
    execute = service.executor(actor, uuid4(), [], lambda: actor)
    before_batch = catalog_calls
    result = json.loads(
        execute("home_get_statuses", json.dumps({"device_ids": [first.id, second.id]}))
    )
    assert result[0]["status"]["on"] is True
    assert result[1] == {"device_id": second.id, "error": "Device is unavailable"}
    # Tool availability checks the catalogue once; the batch itself loads it once.
    assert catalog_calls - before_batch == 2


def test_batch_validates_scope_and_all_targets_before_network(home_setup):
    service, actor, _ = home_setup
    service.home.lifx.read = lambda d: pytest.fail("No network reads for invalid targets")
    query = HomeStatusQuery(device_ids=(service.home.devices[0].id, "missing-device"))
    with pytest.raises(NotFoundError):
        service.home.read_many(actor, query)
    with pytest.raises(AuthorizationError):
        service.home.read_many(actor.model_copy(update={"scopes": frozenset()}), query)
    with pytest.raises(NotFoundError):
        service.home.read_many(actor.model_copy(update={"household_id": uuid4()}), query)
    for ids in ([], ["duplicate", "duplicate"], [str(i) for i in range(33)]):
        with pytest.raises(ValidationError):
            HomeStatusQuery(device_ids=ids)


def test_listing_saved_devices_never_waits_for_discovery(home_setup, monkeypatch):
    service, actor, _ = home_setup
    monkeypatch.setattr(
        service.home.catalog, "sync", lambda *a, **k: pytest.fail("Blocking discovery")
    )
    execute = service.executor(actor, uuid4(), [], lambda: actor)
    assert json.loads(execute("home_list_devices", "{}"))[0]["id"] == "office-beam"
