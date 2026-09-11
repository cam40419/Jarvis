import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest

from jarvis.config import Settings
from jarvis.domain.conversations import CreateThread, SubmitRun
from jarvis.domain.errors import AuthenticationError, ModelBusyError, ModelError
from jarvis.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from jarvis.domain.model import ModelAnswer
from jarvis.domain.models import ActorContext, Channel, utc_now
from jarvis.services.audit import AuditService
from jarvis.services.model_conversations import ModelConversationService


class FakeModel:
    def __init__(self):
        self.requests = []
        self.action = lambda: None

    def generate(self, request):
        self.requests.append(request)
        self.action()
        return ModelAnswer(
            text="A useful answer.",
            response_id="resp_test",
            model="fake-snapshot",
            input_tokens=50,
            output_tokens=8,
        )


@pytest.fixture
def model_setup(store):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=frozenset({"threads:read", "threads:write", "memories:read"}),
    )
    model = FakeModel()
    service = ModelConversationService(store, AuditService(store), model, Settings())
    thread = service.create(
        actor, CreateThread(title="Assistant", idempotency_key="model-thread-001")
    )
    return service, actor, model, thread


def body(key="model-request-001"):
    return SubmitRun(text="Help me plan dinner", idempotency_key=key)


def test_model_success_snapshot_and_retry(model_setup):
    service, actor, model, thread = model_setup
    run = service.submit(actor, thread.id, body())
    assert service.submit(actor, thread.id, body()) == run
    assert len(model.requests) == 1
    assert run.model_provider == "openai" and run.provider_model == "fake-snapshot"
    assert run.input_tokens == 50 and run.output_tokens == 8
    assert run.model_request == model.requests[0]
    assert json.loads(run.model_request.input_text)["messages"][-1]["text"] == body().text
    assert service.messages(actor, thread.id, 0, 100)[1].text == "A useful answer."
    assert len(service.events(actor, run.id, 2)) == 2
    assert service.store.attempt(run.id).status == "succeeded"
    assert service.store.pending_attempt(thread.id) is None
    from jarvis.services.conversations import ConversationService

    assert ConversationService(service.store, service.audit).submit(actor, thread.id, body()) == run


def test_network_call_releases_transaction_and_concurrent_retry(model_setup):
    service, actor, model, thread = model_setup
    entered, release = Event(), Event()

    def hold():
        entered.set()
        assert release.wait(10)

    model.action = hold
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(service.submit, actor, thread.id, body())
        assert entered.wait(5)
        try:
            # This would deadlock or time out if generation held the household transaction.
            assert service.get(actor, thread.id) == thread
            with pytest.raises(ModelBusyError):
                service.submit(actor, thread.id, body())
            with pytest.raises(ModelBusyError):
                service.submit(actor, thread.id, body("different-request"))
        finally:
            release.set()
        run = future.result(timeout=5)
    assert len(model.requests) == 1
    assert service.run(actor, run.id) == run


def test_model_failure_no_messages_and_no_automatic_retry(model_setup):
    service, actor, model, thread = model_setup

    def fail():
        raise ModelError("model_rate_limit")

    model.action = fail
    for _ in range(2):
        with pytest.raises(ModelError, match="quota"):
            service.submit(actor, thread.id, body())
    assert len(model.requests) == 1
    from jarvis.services.conversations import ConversationService

    with pytest.raises(ModelError):
        ConversationService(service.store, service.audit).submit(actor, thread.id, body())
    assert service.messages(actor, thread.id, 0, 100) == ()
    assert service.store.pending_attempt(thread.id) is None
    assert service.store.audit_events()[-1].payload == {"error_code": "model_rate_limit"}
    model.action = lambda: None
    assert service.submit(actor, thread.id, body("new-request-key")).status == "succeeded"


def test_crash_expiry_is_not_reexecuted(model_setup):
    service, actor, model, thread = model_setup

    def crash():
        raise SystemExit("process crash")

    model.action = crash
    with pytest.raises(SystemExit):
        service.submit(actor, thread.id, body())
    attempt = service.store.pending_attempt(thread.id)
    assert attempt is not None
    from jarvis.services.conversations import ConversationService

    with pytest.raises(ModelBusyError):
        ConversationService(service.store, service.audit).submit(actor, thread.id, body())
    with service.store.transaction(actor.household_id):
        service.store.save_attempt(
            attempt.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
        )
    with pytest.raises(ModelError, match="timed out"):
        service.submit(actor, thread.id, body())
    assert len(model.requests) == 1
    assert service.store.attempt(attempt.run.id).status == "failed"


def test_session_revoked_during_generation_does_not_publish(model_setup):
    service, actor, model, thread = model_setup

    def revoked():
        raise AuthenticationError("session revoked")

    with pytest.raises(ModelError, match="Access changed"):
        service.submit(actor, thread.id, body(), revalidate=revoked)
    assert len(model.requests) == 1
    assert service.messages(actor, thread.id, 0, 100) == ()


def test_final_commit_failure_rolls_back_and_does_not_call_model_again(model_setup, monkeypatch):
    service, actor, model, thread = model_setup
    original = service.audit.record

    def fail(**kwargs):
        original(**kwargs)
        if kwargs["event_type"] == "run.completed":
            raise RuntimeError("commit failure")

    monkeypatch.setattr(service.audit, "record", fail)
    with pytest.raises(RuntimeError):
        service.submit(actor, thread.id, body())
    assert service.messages(actor, thread.id, 0, 100) == ()
    assert service.store.run(service.store.pending_attempt(thread.id).run.id) is None
    with pytest.raises(ModelBusyError):
        service.submit(actor, thread.id, body())
    assert len(model.requests) == 1


def test_existing_local_replay_survives_model_enable(model_setup):
    from jarvis.services.conversations import ConversationService

    service, actor, model, thread = model_setup
    old = ConversationService(service.store, service.audit).submit(actor, thread.id, body())
    assert service.submit(actor, thread.id, body()) == old
    assert not model.requests


def test_late_provider_answer_cannot_publish_after_expiry(model_setup):
    service, actor, model, thread = model_setup

    def expire():
        attempt = service.store.pending_attempt(thread.id)
        with service.store.transaction(actor.household_id):
            service.store.save_attempt(
                attempt.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
            )

    model.action = expire
    with pytest.raises(ModelError, match="timed out"):
        service.submit(actor, thread.id, body())
    assert service.messages(actor, thread.id, 0, 100) == ()
