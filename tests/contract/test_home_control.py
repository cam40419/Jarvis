import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest

from simon.adapters.google import ConnectedError
from simon.domain.conversations import CreateThread, Message, ModelAttempt, Run, SubmitRun
from simon.domain.errors import AuthorizationError, ModelError, NotFoundError
from simon.domain.home import HomeControl, HomeStatus
from simon.domain.models import utc_now
from simon.services.interaction import InteractionService
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_home_tools import home_setup as fixture_home_setup
from tests.contract.test_model_runs import FakeModel


@pytest.fixture
def home_setup(store, tmp_path):
    return fixture_home_setup.__wrapped__(store, tmp_path)


def pending(service, actor):
    thread = service.conversations.create(
        actor, CreateThread(title="Lights", idempotency_key=str(uuid4()))
    )
    user = Message(thread_id=thread.id, sequence=1, role="user", text="All off")
    run = Run(
        thread_id=thread.id,
        actor_id=actor.actor_id,
        context=(),
        input_message_id=user.id,
        output_message_id=uuid4(),
    )
    attempt = ModelAttempt(
        run=run,
        user=user,
        household_id=actor.household_id,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    service.store.save_attempt(attempt)
    return attempt


def test_all_off_immediate_mixed_receipts_and_no_repeat(home_setup):
    service, actor, _ = home_setup
    light = service.home.devices[0]
    service.home.devices = (
        light,
        light.model_copy(update={"id": "arrow-up", "name": "Up Arrow"}),
        light.model_copy(update={"id": "purifier", "load_type": "air_purifier"}),
    )
    calls = []

    def send(device, change):
        calls.append(device.id)
        assert change.on is False
        if device.id == "arrow-up":
            raise ConnectedError("Timeout", unknown=True)

    service.home.lifx.set = send
    service.home.lifx.read = lambda d: HomeStatus(device_id=d.id, on=False, capabilities=("power",))
    attempt = pending(service, actor)
    request = HomeControl(all_lights=True, on=False)
    result = service.home.control(actor, attempt.run.id, request, lambda: actor)
    assert sorted(calls) == ["arrow-up", "office-beam"]
    assert [c.status for c in result] == ["succeeded", "unknown"]
    assert result[0].verified and not result[1].verified
    assert service.home.control(actor, attempt.run.id, request, lambda: actor) == result
    assert len(calls) == 2
    assert len(service.home.commands(actor)) == 2
    assert service.home.commands(actor.model_copy(update={"actor_id": uuid4()})) == ()
    assert service.home.commands(actor.model_copy(update={"household_id": uuid4()})) == ()


@pytest.mark.parametrize(
    "target", [{"room": "office"}, {"group": "Desk"}, {"device_ids": ["office-beam"]}]
)
def test_room_group_and_explicit_color_targets(home_setup, target):
    service, actor, _ = home_setup
    service.home.devices = (service.home.devices[0].model_copy(update={"groups": ("Desk",)}),)
    calls = []
    service.home.lifx.set = lambda d, c: calls.append(c)
    service.home.lifx.read = lambda d: HomeStatus(
        device_id=d.id, color="#0000ff", brightness=40, capabilities=("color", "brightness")
    )
    result = service.home.control(
        actor,
        pending(service, actor).run.id,
        HomeControl(**target, color="#0000ff", brightness=40),
        lambda: actor,
    )
    assert len(calls) == 1 and calls[0].on is None
    assert result[0].verified


def test_inactive_revoked_missing_and_unsupported_never_dispatch(home_setup):
    service, actor, _ = home_setup
    service.home.lifx.set = lambda *a: pytest.fail("must not send")
    request = HomeControl(all_lights=True, on=False)
    attempt = pending(service, actor)
    with pytest.raises(NotFoundError):
        service.home.control(
            actor, attempt.run.id, HomeControl(room="Missing", on=False), lambda: actor
        )
    with pytest.raises(AuthorizationError):
        service.home.control(
            actor, attempt.run.id, request, lambda: actor.model_copy(update={"scopes": frozenset()})
        )
    service.home.lifx.read = lambda d: HomeStatus(device_id=d.id, online=False)
    result = service.home.control(actor, attempt.run.id, request, lambda: actor)
    assert result[0].status == "failed"
    service.store.save_attempt(attempt.model_copy(update={"status": "failed"}))
    with pytest.raises(AuthorizationError):
        service.home.control(actor, attempt.run.id, request, lambda: actor)


def test_cancel_during_read_prevents_write(home_setup):
    service, actor, _ = home_setup
    attempt = pending(service, actor)

    def read(device):
        service.store.save_attempt(attempt.model_copy(update={"status": "failed"}))
        return HomeStatus(device_id=device.id, capabilities=("power",))

    service.home.lifx.read = read
    service.home.lifx.set = lambda *a: pytest.fail("cancelled before dispatch")
    result = service.home.control(
        actor, attempt.run.id, HomeControl(all_lights=True, on=False), lambda: actor
    )
    assert result[0].status == "failed"


def test_concurrent_retry_returns_saved_claim_without_database_lock(home_setup):
    service, actor, _ = home_setup
    attempt = pending(service, actor)
    entered, release = Event(), Event()
    calls = []

    def send(*args):
        calls.append(1)
        entered.set()
        assert release.wait(10)

    service.home.lifx.set = send
    request = HomeControl(all_lights=True, on=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.home.control, actor, attempt.run.id, request, lambda: actor)
        assert entered.wait(10)
        try:
            second = pool.submit(
                service.home.control, actor, attempt.run.id, request, lambda: actor
            )
            assert second.result(timeout=5)[0].status == "executing"
        finally:
            release.set()
        assert first.result(timeout=5)[0].status == "succeeded"
    assert calls == [1]


@pytest.mark.parametrize("fail_answer", [False, True])
def test_chat_tool_executes_and_receipt_survives_failed_answer(home_setup, fail_answer):
    service, actor, _ = home_setup
    calls, outputs = [], []
    service.home.lifx.set = lambda d, c: calls.append(c)
    model = FakeModel()

    def generate(request, delta, execute):
        assert "home_control" in request.tools and "propose_home_change" not in request.tools
        result = json.loads(execute("home_control", '{"all_lights":true,"on":false}'))
        outputs.append(result)
        assert result["requires_confirmation"] is False
        assert result["commands"][0]["status"] == "succeeded"
        if fail_answer:
            raise ModelError()
        return model.generate(request)

    model.generate_with_tools = generate
    conversations = ModelConversationService(
        service.store, service.audit, model, service.settings, service
    )
    thread = conversations.create(actor, CreateThread(title="Lights", idempotency_key=str(uuid4())))
    submit = SubmitRun(text="All off", idempotency_key=str(uuid4()))
    if fail_answer:
        with pytest.raises(ModelError):
            conversations.submit(actor, thread.id, submit)
    else:
        run = conversations.submit(actor, thread.id, submit)
        assert not run.action_ids
        answers = InteractionService(service.store, service.audit).answers(actor, thread.id)
        assert answers[0].home_commands[0].status == "succeeded"
        assert conversations.submit(actor, thread.id, submit).id == run.id
    assert len(calls) == 1 and len(service.home.commands(actor)) == 1
    if not fail_answer:

        def deeper(request, delta, execute):
            assert "home_control" not in request.tools
            return model.generate(request)

        model.generate_with_tools = deeper
        conversations.submit(
            actor,
            thread.id,
            SubmitRun(
                text=submit.text, parent_run_id=run.id, profile="deep", idempotency_key=str(uuid4())
            ),
        )
        assert len(calls) == 1


def test_reconsidering_an_answer_cannot_repeat_device_commands(home_setup):
    service, actor, _ = home_setup
    attempt = pending(service, actor)
    service.store.save_attempt(
        attempt.model_copy(
            update={"run": attempt.run.model_copy(update={"parent_run_id": uuid4()})}
        )
    )
    service.home.lifx.set = lambda *a: pytest.fail("Think deeper must not dispatch")
    with pytest.raises(AuthorizationError):
        service.home.control(
            actor, attempt.run.id, HomeControl(all_lights=True, on=False), lambda: actor
        )
