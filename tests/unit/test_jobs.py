from uuid import uuid4

import pytest

from jarvis.adapters.memory import InMemoryStore
from jarvis.domain.errors import IdempotencyConflictError, InvalidTransitionError
from jarvis.domain.models import ActorContext, Channel, JobStatus
from jarvis.services.audit import AuditService
from jarvis.services.jobs import JobService


def setup() -> tuple[JobService, InMemoryStore, ActorContext]:
    store = InMemoryStore()
    actor = ActorContext(actor_id=uuid4(), household_id=uuid4(), channel=Channel.API)
    return JobService(store, AuditService(store)), store, actor


def test_job_submission_is_idempotent() -> None:
    service, store, actor = setup()
    first, created = service.submit(
        actor, kind="test.job", input={"a": 1}, idempotency_key="job-key-123"
    )
    replay, replay_created = service.submit(
        actor, kind="test.job", input={"a": 1}, idempotency_key="job-key-123"
    )
    assert created is True
    assert replay_created is False
    assert replay.id == first.id
    assert len(store.audit_events()) == 1


def test_job_key_conflict_is_rejected() -> None:
    service, _, actor = setup()
    service.submit(actor, kind="test.job", input={"a": 1}, idempotency_key="job-key-123")
    with pytest.raises(IdempotencyConflictError):
        service.submit(actor, kind="test.job", input={"a": 2}, idempotency_key="job-key-123")


def test_job_transitions_use_optimistic_versioning_and_terminal_states() -> None:
    service, _, actor = setup()
    job, _ = service.submit(
        actor, kind="test.job", input={"a": 1}, idempotency_key="job-key-123"
    )
    running = service.transition(
        actor, job.id, expected_version=1, status=JobStatus.RUNNING
    )
    assert running.version == 2

    with pytest.raises(InvalidTransitionError):
        service.transition(actor, job.id, expected_version=1, status=JobStatus.FAILED)

    complete = service.transition(
        actor,
        job.id,
        expected_version=2,
        status=JobStatus.SUCCEEDED,
        result={"ok": True},
    )
    assert complete.result == {"ok": True}

    with pytest.raises(InvalidTransitionError):
        service.transition(actor, job.id, expected_version=3, status=JobStatus.RUNNING)

