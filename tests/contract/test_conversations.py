from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from jarvis.domain.conversations import CreateThread, SubmitRun
from jarvis.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
)
from jarvis.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID
from jarvis.domain.models import ActorContext, Channel
from jarvis.services.audit import AuditService
from jarvis.services.conversations import ConversationService


@pytest.fixture
def actor():
    return ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=frozenset({"threads:read", "threads:write"}),
    )


@pytest.fixture
def service(store):
    return ConversationService(store, AuditService(store))


def create(service, actor):
    return service.create(actor, CreateThread(title="Test", idempotency_key="thread-test-001"))


def submit(service, actor, thread, text="Hello", key="run-test-001"):
    return service.submit(actor, thread.id, SubmitRun(text=text, idempotency_key=key))


def test_history_snapshot_and_replay(service, actor):
    thread = create(service, actor)
    assert create(service, actor) == thread
    first = submit(service, actor, thread)
    second = submit(service, actor, thread, text="Next", key="run-test-002")
    assert len(first.context) == 1
    assert len(second.context) == 3
    assert second.context[0].source_message_id == first.input_message_id
    assert all(c.trust == "untrusted" for c in second.context)
    assert first.capability_manifest == ()
    assert service.run(actor, first.id) == first
    assert submit(service, actor, thread) == first
    assert [m.sequence for m in service.messages(actor, thread.id, 1, 2)] == [2, 3]
    assert len(service.messages(actor, thread.id, 0, 100)) == 4
    assert len(service.store.audit_events()) == len(service.store.outbox_events()) == 3
    assert [e.sequence for e in service.events(actor, first.id, 2)] == [3, 4]
    assert service.events(actor, first.id, 4) == ()
    assert service.list(actor, 0, 1) == (thread,)
    assert service.list(actor, 1, 1) == ()
    with pytest.raises(ValidationError):
        service.events(actor, first.id, 5)
    with pytest.raises(IdempotencyConflictError):
        submit(service, actor, thread, text="changed")
    with pytest.raises(IdempotencyConflictError):
        service.create(actor, CreateThread(title="changed", idempotency_key="thread-test-001"))


def test_household_and_scope_boundaries(service, actor):
    thread = create(service, actor)
    run = submit(service, actor, thread)
    other = actor.model_copy(update={"household_id": uuid4()})
    assert service.list(other, 0, 100) == ()
    for operation in (
        lambda: service.get(other, thread.id),
        lambda: service.messages(other, thread.id, 0, 100),
        lambda: submit(service, other, thread),
        lambda: service.run(other, run.id),
        lambda: service.events(other, run.id, 0),
        lambda: service.run(actor, uuid4()),
    ):
        with pytest.raises(NotFoundError):
            operation()
    denied = actor.model_copy(update={"scopes": frozenset()})
    with pytest.raises(AuthorizationError):
        create(service, denied)
    with pytest.raises(AuthorizationError):
        service.list(denied, 0, 100)


def test_concurrent_retry_and_rollback(service, actor, monkeypatch):
    thread = create(service, actor)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: submit(service, actor, thread), range(8)))
    assert len({r.id for r in results}) == 1
    assert len(service.messages(actor, thread.id, 0, 100)) == 2
    original = service.audit.record

    def fail(**kwargs):
        original(**kwargs)
        raise RuntimeError("injected crash")

    monkeypatch.setattr(service.audit, "record", fail)
    with pytest.raises(RuntimeError):
        submit(service, actor, thread, key="rollback-run-001")
    assert len(service.messages(actor, thread.id, 0, 100)) == 2
    assert len(service.store.audit_events()) == len(service.store.outbox_events()) == 2
    monkeypatch.setattr(service.audit, "record", original)
    submit(service, actor, thread, key="rollback-run-001")
    assert len(service.messages(actor, thread.id, 0, 100)) == 4


def test_long_threads_use_bounded_context_and_retry_still_works(service, actor):
    thread = create(service, actor)
    first = submit(service, actor, thread)
    for i in range(49):
        submit(service, actor, thread, key=f"context-limit-{i}")
    run = submit(service, actor, thread, key="context-limit-exceeded")
    assert len(run.context) == 17
    assert run.context_policy.omitted_messages == 68
    assert len(run.summary_context.source_message_ids) == 16
    assert service.messages(actor, thread.id, 100, 100)[-1].sequence == 102
    assert submit(service, actor, thread) == first
