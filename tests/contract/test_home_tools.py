import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest

from simon.adapters.google import ConnectedError
from simon.domain.conversations import CreateThread, SubmitRun
from simon.domain.errors import AuthorizationError, NotFoundError
from simon.domain.home import HomeChange, HomeStatus
from simon.domain.models import utc_now
from simon.services.connected import ConnectedService
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_connected import connected_setup
from tests.contract.test_model_runs import FakeModel


@pytest.fixture
def home_setup(store, tmp_path):
    service, actor, token = connected_setup(store)
    inventory = tmp_path / "home.json"
    inventory.write_text(
        json.dumps(
            [
                {
                    "id": "office-beam",
                    "household_id": str(actor.household_id),
                    "name": "Office Beam",
                    "room": "Office",
                    "provider": "lifx",
                    "remote_id": "d073d5000001",
                    "load_type": "lighting",
                    "control_enabled": True,
                }
            ]
        )
    )
    settings = service.settings.model_copy(update={"home_devices_file": inventory})
    service = ConnectedService(store, service.audit, settings, service.identity)
    service.home.lifx.read = lambda device: HomeStatus(
        device_id=device.id,
        on=True,
        brightness=50,
        online=True,
        capabilities=("power", "brightness"),
    )
    return service, actor, token


def prepare(service, actor):
    model = FakeModel()
    model.generate_with_tools = lambda request, delta, execute: model.generate(request)

    conversations = ModelConversationService(
        service.store, service.audit, model, service.settings, service
    )
    thread = conversations.create(
        actor, CreateThread(title="Office lights", idempotency_key=str(uuid4()))
    )
    run = conversations.submit(
        actor, thread.id, SubmitRun(text="Set the Beam to 50%", idempotency_key=str(uuid4()))
    )
    # Legacy saved previews remain readable/cancellable through the old API.
    action = service.home.propose(
        actor, run.id, HomeChange(device_id="office-beam", on=True, brightness=50)
    )
    service.store.save_action(action)
    return service.get_action(actor, action.id)


def test_legacy_preview_confirm_and_verified_receipt(home_setup):
    service, actor, _ = home_setup
    calls = []
    service.home.lifx.set = lambda device, change: calls.append(change)
    action = prepare(service, actor)
    assert not calls and action.kind == "home.set" and action.device_name == "Office Beam"
    completed = service.decide(actor, action.id, confirm=True, revalidate=lambda: actor)
    assert completed.status == "succeeded" and completed.home_verified
    assert service.decide(actor, action.id, confirm=True, revalidate=lambda: actor) == completed
    assert len(calls) == 1 and calls[0] == action.home


def test_inventory_scope_and_disabled_loads(home_setup):
    service, actor, _ = home_setup
    other = actor.model_copy(update={"household_id": uuid4()})
    assert service.home.inventory(other) == []
    with pytest.raises(NotFoundError):
        service.home.read(other, "office-beam")
    with pytest.raises(AuthorizationError):
        service.home.inventory(actor.model_copy(update={"scopes": frozenset()}))
    device = service.home.devices[0]
    for changes in [
        {"control_enabled": False},
        {"load_type": "unclassified"},
        {"load_type": "other"},
    ]:
        service.home.devices = (device.model_copy(update=changes),)
        with pytest.raises(AuthorizationError):
            service.home.propose(actor, uuid4(), HomeChange(device_id=device.id, on=True))
    service.home.devices = (device.model_copy(update={"load_type": "air_purifier"}),)
    assert (
        service.home.propose(actor, uuid4(), HomeChange(device_id=device.id, on=True)).kind
        == "home.set"
    )


def test_config_changed_or_revoked_blocks_confirmation(home_setup):
    service, actor, _ = home_setup
    action = prepare(service, actor)
    service.home.lifx.set = lambda *args: pytest.fail("must not switch")
    with pytest.raises(AuthorizationError):
        service.decide(
            actor,
            action.id,
            confirm=True,
            revalidate=lambda: actor.model_copy(update={"scopes": frozenset({"threads:read"})}),
        )
    service.home.devices = (
        service.home.devices[0].model_copy(update={"remote_id": "d073d5000002"}),
    )
    with pytest.raises(AuthorizationError, match="configuration changed"):
        service.decide(actor, action.id, confirm=True, revalidate=lambda: actor)
    assert (
        service.decide(actor, action.id, confirm=False, revalidate=lambda: actor).status
        == "cancelled"
    )


def test_unknown_command_and_unverified_state_are_not_retried(home_setup):
    service, actor, _ = home_setup
    action = prepare(service, actor)
    calls = []

    def fail(*args):
        calls.append(1)
        raise ConnectedError("Timeout", unknown=True)

    service.home.lifx.set = fail
    assert (
        service.decide(actor, action.id, confirm=True, revalidate=lambda: actor).status == "unknown"
    )
    service.decide(actor, action.id, confirm=True, revalidate=lambda: actor)
    assert calls == [1]
    action = prepare(service, actor)
    service.home.lifx.set = lambda *args: None
    service.home.lifx.read = lambda device: HomeStatus(device_id=device.id, on=False)
    result = service.decide(actor, action.id, confirm=True, revalidate=lambda: actor)
    assert result.status == "succeeded" and not result.home_verified


def test_concurrent_home_confirmation_does_not_hold_transaction(home_setup):
    service, actor, _ = home_setup
    action = prepare(service, actor)
    entered, release = Event(), Event()

    def wait(*args):
        entered.set()
        assert release.wait(10)

    service.home.lifx.set = wait
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            service.decide, actor, action.id, confirm=True, revalidate=lambda: actor
        )
        assert entered.wait(10)
        try:
            second = pool.submit(
                service.decide, actor, action.id, confirm=True, revalidate=lambda: actor
            )
            assert second.result(timeout=5).status == "executing"
        finally:
            release.set()
        assert first.result(timeout=5).status == "succeeded"


@pytest.mark.parametrize("confirm,expired", [(False, False), (True, True)])
def test_cancel_or_expiry_never_operates_device(home_setup, confirm, expired):
    service, actor, _ = home_setup
    action = prepare(service, actor)
    if expired:
        service.store.save_action(
            action.model_copy(
                update={
                    "expires_at": utc_now() - timedelta(seconds=1),
                }
            )
        )
    service.home.lifx.set = lambda *args: pytest.fail("must not operate device")
    assert (
        service.decide(actor, action.id, confirm=confirm, revalidate=lambda: actor).status
        == "cancelled"
    )


@pytest.mark.parametrize("failure", ["readback", "offline", "rejected", "unexpected"])
def test_unverified_and_failed_command_receipts(home_setup, failure):
    service, actor, _ = home_setup
    action = prepare(service, actor)

    def operate(*args):
        if failure == "rejected":
            raise ConnectedError("Rejected")
        if failure == "unexpected":
            raise RuntimeError("private credential material")

    def read(device):
        if failure == "readback":
            raise ConnectedError("Disconnected")
        return HomeStatus(device_id=device.id, online=False, on=True, brightness=50)

    service.home.lifx.set, service.home.lifx.read = operate, read
    result = service.decide(actor, action.id, confirm=True, revalidate=lambda: actor)
    expected = {
        "readback": "succeeded",
        "offline": "succeeded",
        "rejected": "failed",
        "unexpected": "unknown",
    }
    assert result.status == expected[failure] and not result.home_verified
    assert "credential" not in result.model_dump_json()


def test_preflight_offline_unsupported_and_malformed_status(home_setup):
    service, actor, _ = home_setup
    change = HomeChange(device_id="office-beam", brightness=50)
    service.home.lifx.read = lambda device: HomeStatus(device_id=device.id, online=False)
    with pytest.raises(ConnectedError, match="offline"):
        service.home.propose(actor, uuid4(), change)
    service.home.lifx.read = lambda device: HomeStatus(device_id=device.id)
    with pytest.raises(ConnectedError, match="not supported"):
        service.home.propose(actor, uuid4(), change)

    def malformed(*args):
        raise KeyError("private provider field")

    service.home.lifx.read = malformed
    with pytest.raises(ConnectedError, match="could not be interpreted"):
        service.home.read(actor, "office-beam")


def test_revocation_after_claim_never_dispatches(home_setup):
    service, actor, _ = home_setup
    action = prepare(service, actor)
    checks = []

    def revalidate():
        checks.append(1)
        return actor if len(checks) == 1 else actor.model_copy(update={"scopes": frozenset()})

    service.home.lifx.set = lambda *args: pytest.fail("must not operate device")
    result = service.decide(actor, action.id, confirm=True, revalidate=revalidate)
    assert result.status == "failed" and checks == [1, 1]
