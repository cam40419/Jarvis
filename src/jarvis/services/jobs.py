from typing import Any
from uuid import UUID

from jarvis.domain.errors import InvalidTransitionError, NotFoundError
from jarvis.domain.models import (
    TERMINAL_JOB_STATUSES,
    ActorContext,
    Job,
    JobStatus,
)
from jarvis.domain.ports import JobStore
from jarvis.services.audit import AuditService
from jarvis.services.canonical import digest

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.WAITING,
            JobStatus.NEEDS_HUMAN,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.WAITING: frozenset(
        {JobStatus.RUNNING, JobStatus.NEEDS_HUMAN, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.NEEDS_HUMAN: frozenset({JobStatus.RUNNING, JobStatus.FAILED, JobStatus.CANCELLED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class JobService:
    def __init__(self, store: JobStore, audit: AuditService) -> None:
        self._store = store
        self._audit = audit

    def submit(
        self,
        actor: ActorContext,
        *,
        kind: str,
        input: dict[str, Any],
        idempotency_key: str,
    ) -> tuple[Job, bool]:
        job = Job(
            household_id=actor.household_id,
            created_by=actor.actor_id,
            kind=kind,
            idempotency_key=idempotency_key,
            input=input,
            input_digest=digest(input),
        )
        with self._store.transaction(actor.household_id):
            saved, created = self._store.create_job(job)
            if created:
                self._audit.record(
                    event_type="job.created",
                    actor=actor,
                    resource_type="job",
                    resource_id=str(saved.id),
                    payload={"kind": saved.kind},
                )
            return saved, created

    def transition(
        self,
        actor: ActorContext,
        job_id: UUID,
        *,
        expected_version: int,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> Job:
        with self._store.transaction(actor.household_id):
            current = self._store.get_job(job_id)
            if current is None or current.household_id != actor.household_id:
                raise NotFoundError("job not found")
            if (
                current.status in TERMINAL_JOB_STATUSES
                or status not in ALLOWED_TRANSITIONS[current.status]
            ):
                raise InvalidTransitionError(f"cannot transition {current.status} to {status}")
            updated = self._store.transition_job(
                job_id, expected_version, status, result, error_code
            )
            self._audit.record(
                event_type="job.transitioned",
                actor=actor,
                resource_type="job",
                resource_id=str(job_id),
                payload={"from": current.status.value, "to": updated.status.value},
            )
            return updated

    def get(self, actor: ActorContext, job_id: UUID) -> Job:
        job = self._store.get_job(job_id)
        if job is None or job.household_id != actor.household_id:
            raise NotFoundError("job not found")
        return job
