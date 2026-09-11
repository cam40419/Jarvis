from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from uuid import uuid4

import pytest

from jarvis.api.app import AppContainer
from jarvis.domain.errors import IdempotencyConflictError, InvalidTransitionError, NotFoundError
from jarvis.domain.models import ActorContext, CapabilityInvocation, Channel, JobStatus
from jarvis.domain.ports import Store
from jarvis.services.audit import AuditService
from jarvis.services.jobs import JobService


@pytest.fixture
def actor() -> ActorContext:
    return ActorContext(
        actor_id="11111111-1111-4111-8111-111111111111",
        household_id="22222222-2222-4222-8222-222222222222",
        scopes=frozenset({"system:read"}),
        channel=Channel.API,
    )


def submit(store: Store, actor: ActorContext, key: str = "contract-job-001"):
    return JobService(store, AuditService(store)).submit(
        actor, kind="test.job", input={"nested": {"value": 1}}, idempotency_key=key
    )[0]


def test_job_replay_conflict_and_isolated_return_values(store: Store, actor: ActorContext) -> None:
    first = submit(store, actor)
    first.input["nested"]["value"] = 99
    replay = submit(store, actor)
    assert replay.id == first.id
    assert replay.input["nested"]["value"] == 1
    with pytest.raises(IdempotencyConflictError):
        JobService(store, AuditService(store)).submit(
            actor, kind="test.job", input={"value": 2}, idempotency_key="contract-job-001"
        )
    assert len(store.audit_events()) == len(store.outbox_events()) == 1


def test_concurrent_submissions_commit_one_job_and_event(store: Store, actor: ActorContext) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(lambda _: submit(store, actor), range(8)))
    assert len({job.id for job in jobs}) == 1
    assert len(store.audit_events()) == len(store.outbox_events()) == 1


def test_job_versions_and_household_boundary(store: Store, actor: ActorContext) -> None:
    service = JobService(store, AuditService(store))
    job = submit(store, actor)
    other = actor.model_copy(update={"household_id": uuid4()})
    with pytest.raises(NotFoundError):
        service.get(other, job.id)
    with pytest.raises(NotFoundError):
        service.transition(other, job.id, expected_version=1, status=JobStatus.RUNNING)
    running = service.transition(actor, job.id, expected_version=1, status=JobStatus.RUNNING)
    assert running.version == 2
    with pytest.raises(InvalidTransitionError):
        service.transition(actor, job.id, expected_version=1, status=JobStatus.FAILED)
    service.transition(actor, job.id, expected_version=2, status=JobStatus.SUCCEEDED)
    with pytest.raises(InvalidTransitionError):
        service.transition(actor, job.id, expected_version=3, status=JobStatus.RUNNING)
    assert len(store.outbox_events()) == 3


def test_rollback_removes_job_audit_and_outbox(store: Store, actor: ActorContext) -> None:
    with pytest.raises(RuntimeError, match="crash"), store.transaction(actor.household_id):
        job = submit(store, actor)
        raise RuntimeError("crash")
    assert store.get_job(job.id) is None
    assert store.audit_events() == ()
    assert store.outbox_events() == ()
    assert submit(store, actor).id != job.id


def test_audit_failure_rolls_back_submission(
    store: Store,
    actor: ActorContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_event):
        raise RuntimeError("audit unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(store, "append_audit", fail)
        with pytest.raises(RuntimeError):
            submit(store, actor)
    assert store.outbox_events() == ()
    submit(store, actor)
    assert len(store.audit_events()) == 1


def test_concurrent_distinct_requests_have_contiguous_audit(
    store: Store, actor: ActorContext
) -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: submit(store, actor, f"distinct-job-{i}"), range(8)))
    events = store.audit_events(actor.household_id)
    assert [event.sequence for event in events] == list(range(1, 9))
    assert events[0].previous_hash == "0" * 64
    for previous, current in pairwise(events):
        assert current.previous_hash == previous.event_hash


def test_invocation_replay_preserves_identity_and_is_atomic(
    store: Store, actor: ActorContext
) -> None:
    app = AppContainer(store=store)
    invocation = CapabilityInvocation(
        capability="system.echo",
        arguments={"message": "hello"},
        idempotency_key="echo-contract-001",
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: app.capabilities.invoke(actor, invocation), range(8)))
    assert sum(not result.replayed for result in results) == 1
    assert len({result.invocation_id for result in results}) == 1
    assert len({result.completed_at for result in results}) == 1
    assert len(store.audit_events()) == len(store.outbox_events()) == 1
    with pytest.raises(IdempotencyConflictError):
        app.capabilities.invoke(
            actor, invocation.model_copy(update={"arguments": {"message": "new"}})
        )


def test_failed_invocation_can_retry(store: Store, actor: ActorContext) -> None:
    with pytest.raises(RuntimeError), store.transaction(actor.household_id):
        store.execute_once("test", "request-1", "a" * 64, lambda: {"ok": True})
        raise RuntimeError("rollback")
    output, replayed = store.execute_once("test", "request-1", "b" * 64, lambda: {"ok": False})
    assert output == {"ok": False}
    assert replayed is False


def test_outbox_delivery_retries_and_marks_success(store: Store, actor: ActorContext) -> None:
    submit(store, actor)
    attempts = []

    def fail(event):
        attempts.append(event.id)
        raise RuntimeError("consumer offline")

    assert store.publish_pending(fail) == 0
    assert store.outbox_events()[0].attempts == 1
    assert store.outbox_events()[0].published_at is None
    assert store.publish_pending(lambda event: attempts.append(event.id)) == 1
    assert attempts[0] == attempts[1]
    assert store.outbox_events()[0].attempts == 2
    assert store.outbox_events()[0].published_at is not None
    assert store.publish_pending(lambda event: attempts.append(event.id)) == 0
    with pytest.raises(ValueError):
        store.publish_pending(lambda _: None, limit=0)


def test_two_publishers_do_not_deliver_same_pending_event(
    store: Store, actor: ActorContext
) -> None:
    from threading import Lock

    seen = []
    lock = Lock()
    for i in range(4):
        submit(store, actor, f"publish-job-{i}")

    def deliver(event):
        with lock:
            seen.append(event.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: store.publish_pending(deliver, limit=2), range(2)))
    assert sum(counts) == 4
    assert len(set(seen)) == len(seen) == 4


def test_replay_from_different_actor_is_rejected(store: Store, actor: ActorContext) -> None:
    submit(store, actor)
    other = actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(IdempotencyConflictError):
        submit(store, other)


def test_nested_rollback_preserves_outer_work(store: Store, actor: ActorContext) -> None:
    with store.transaction(actor.household_id):
        first = submit(store, actor)
        with pytest.raises(RuntimeError), store.transaction(actor.household_id):
            second = submit(store, actor, "nested-job-002")
            raise RuntimeError("savepoint")
        third = submit(store, actor, "nested-job-003")
    assert store.get_job(first.id) is not None
    assert store.get_job(second.id) is None
    assert store.get_job(third.id) is not None
    assert len(store.audit_events()) == 2
