import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest

from simon.adapters.google import ConnectedError
from simon.domain.connected_tools import CalendarDraft
from simon.domain.conversations import CreateThread, Message, ModelAttempt, Run, SubmitRun
from simon.domain.errors import AuthorizationError, ModelError, NotFoundError
from simon.domain.models import utc_now
from simon.services.interaction import InteractionService
from simon.services.model_conversations import ModelConversationService
from tests.contract.test_connected import EVENT, connected_setup
from tests.contract.test_model_runs import FakeModel


def pending_calendar(service, actor):
    thread = service.conversations.create(
        actor, CreateThread(title="Calendar", idempotency_key=str(uuid4()))
    )
    user = Message(thread_id=thread.id, sequence=1, role="user", text="Add lunch at noon")
    run = Run(
        thread_id=thread.id,
        actor_id=actor.actor_id,
        context=(),
        input_message_id=user.id,
        output_message_id=uuid4(),
        capability_manifest=("calendar_create_event",),
    )
    attempt = ModelAttempt(
        run=run,
        user=user,
        household_id=actor.household_id,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    service.store.save_attempt(attempt)
    return attempt


def test_calendar_creates_during_tool_call_and_replays_without_writes(store):
    service, actor, _ = connected_setup(store)
    calls = []

    def create(token, action, sender):
        calls.append(action.id)
        assert service.store.action(action.id).status == "executing"
        assert action.immediate and action.calendar.title == "Lunch"
        return "event123", "https://www.google.com/calendar/event?eid=synthetic"

    service.api.execute = create
    model = FakeModel()

    def generate(request, on_delta, execute):
        assert "calendar_create_event" in request.tools
        first = json.loads(execute("calendar_create_event", json.dumps(EVENT)))
        repeated = json.loads(execute("calendar_create_event", json.dumps(EVENT)))
        assert first == repeated
        assert first["created"] and not first["requires_confirmation"]
        return model.generate(request)

    model.generate_with_tools = generate
    conversations = ModelConversationService(store, service.audit, model, service.settings, service)
    thread = conversations.create(
        actor, CreateThread(title="Calendar", idempotency_key="calendar-thread")
    )
    request = SubmitRun(text="Add lunch at noon", idempotency_key="calendar-create")
    run = conversations.submit(actor, thread.id, request)
    assert len(calls) == 1 and run.action_ids == tuple(calls)
    action = service.get_action(actor, calls[0])
    assert action.status == "succeeded"
    assert InteractionService(store, service.audit).answers(actor, thread.id)[0].actions == (
        action,
    )
    assert conversations.submit(actor, thread.id, request).id == run.id
    assert service.decide(actor, action.id, confirm=True, revalidate=lambda: actor) == action
    assert len(calls) == 1


def test_concurrent_calls_and_equivalent_offsets_share_claim(store):
    service, actor, _ = connected_setup(store)
    attempt = pending_calendar(service, actor)
    started, finish = Event(), Event()
    calls = []

    def create(*args):
        calls.append(1)
        started.set()
        assert finish.wait(5)
        return "event123", None

    service.api.execute = create
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(
            service.create_calendar_event,
            actor,
            attempt.run.id,
            CalendarDraft(**EVENT),
            lambda: actor,
        )
        try:
            assert started.wait(5)
            same_time = {**EVENT, "start": "2026-10-01T16:00:00Z", "end": "2026-10-01T17:00:00Z"}
            duplicate = service.create_calendar_event(
                actor,
                attempt.run.id,
                CalendarDraft(**same_time),
                lambda: actor,
            )
            assert duplicate.status == "executing" and calls == [1]
        finally:
            finish.set()
        completed = future.result()
    assert completed.id == duplicate.id and completed.status == "succeeded"
    assert service.actions(actor, attempt.run.thread_id) == (completed,)


@pytest.mark.parametrize("unknown", [False, True])
def test_failed_or_unknown_action_is_not_retried(store, unknown):
    service, actor, _ = connected_setup(store)
    attempt = pending_calendar(service, actor)
    calls = []

    def fail(*args):
        calls.append(1)
        raise ConnectedError("synthetic failure", unknown=unknown)

    service.api.execute = fail
    first = service.create_calendar_event(
        actor, attempt.run.id, CalendarDraft(**EVENT), lambda: actor
    )
    second = service.create_calendar_event(
        actor, attempt.run.id, CalendarDraft(**EVENT), lambda: actor
    )
    assert first == second and calls == [1]
    assert first.status == ("unknown" if unknown else "failed")


@pytest.mark.parametrize("reason", ["expired", "cancelled", "deeper", "scope", "other", "manifest"])
def test_invalid_or_revisited_requests_cannot_create(store, reason):
    service, actor, _ = connected_setup(store)
    attempt = pending_calendar(service, actor)
    current = actor
    if reason == "expired":
        attempt = attempt.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    elif reason == "cancelled":
        attempt = attempt.model_copy(update={"status": "failed"})
    elif reason in {"deeper", "manifest"}:
        change = {"parent_run_id": uuid4()} if reason == "deeper" else {"capability_manifest": ()}
        attempt = attempt.model_copy(update={"run": attempt.run.model_copy(update=change)})
    elif reason == "scope":
        connection = service.connection(actor)
        store.save_google_connection(connection.model_copy(update={"scopes": ()}))
    else:
        current = actor.model_copy(update={"actor_id": uuid4()})
    store.save_attempt(attempt)
    calls = []
    service.api.execute = lambda *args: (calls.append(1) or "event", None)
    with pytest.raises(AuthorizationError):
        service.create_calendar_event(
            actor, attempt.run.id, CalendarDraft(**EVENT), lambda: current
        )
    assert not calls and not service.actions(actor, attempt.run.thread_id)


def test_model_failure_keeps_receipt_and_next_request_context(store):
    service, actor, _ = connected_setup(store)
    service.api.execute = lambda *args: ("event123", None)
    model = FakeModel()

    def generate(request, on_delta, execute):
        result = json.loads(execute("calendar_create_event", json.dumps(EVENT)))
        assert result["created"]
        raise ModelError()

    model.generate_with_tools = generate
    conversations = ModelConversationService(store, service.audit, model, service.settings, service)
    thread = conversations.create(
        actor, CreateThread(title="Calendar", idempotency_key="calendar-thread")
    )
    with pytest.raises(ModelError):
        conversations.submit(
            actor, thread.id, SubmitRun(text="Create lunch", idempotency_key="calendar-first")
        )
    receipt = service.actions(actor, thread.id)[0]
    assert receipt.status == "succeeded" and service.get_action(actor, receipt.id) == receipt
    with pytest.raises(NotFoundError):
        service.get_action(actor.model_copy(update={"actor_id": uuid4()}), receipt.id)

    def followup(request, on_delta, execute):
        receipts = json.loads(request.input_text)["action_receipts"]
        assert receipts[0]["status"] == "succeeded"
        assert receipts[0]["calendar"]["title"] == "Lunch"
        return model.generate(request)

    model.generate_with_tools = followup
    conversations.submit(
        actor, thread.id, SubmitRun(text="Did it create?", idempotency_key="calendar-followup")
    )


def test_stop_after_claim_before_dispatch_and_creation_limit(store):
    service, actor, _ = connected_setup(store)
    attempt = pending_calendar(service, actor)
    checks = []
    calls = []
    service.api.execute = lambda *args: (calls.append(1) or "event", None)

    def revalidate():
        checks.append(1)
        if len(checks) == 2:
            store.save_attempt(attempt.model_copy(update={"status": "failed"}))
        return actor

    action = service.create_calendar_event(
        actor, attempt.run.id, CalendarDraft(**EVENT), revalidate
    )
    assert action.status == "failed" and not calls
    attempt = pending_calendar(service, actor)
    for index in range(3):
        service.create_calendar_event(
            actor,
            attempt.run.id,
            CalendarDraft(**{**EVENT, "title": f"Lunch {index}"}),
            lambda: actor,
        )
    result = service.executor(actor, attempt.run.id, [], lambda: actor)(
        "calendar_create_event",
        json.dumps({**EVENT, "title": "Fourth event"}),
    )
    assert "At most three" in result and len(calls) == 3
