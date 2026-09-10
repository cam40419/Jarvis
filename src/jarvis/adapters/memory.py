from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from jarvis.domain.errors import IdempotencyConflictError, InvalidTransitionError
from jarvis.domain.models import AuditEvent, CapabilityDefinition, Job, JobStatus, utc_now
from jarvis.domain.ports import CapabilityHandler


class InMemoryStore:
    """Thread-safe reference adapter used for tests and local smoke runs."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._capabilities: dict[
            str, tuple[CapabilityDefinition, type[BaseModel], CapabilityHandler]
        ] = {}
        self._invocations: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._jobs: dict[UUID, Job] = {}
        self._job_keys: dict[tuple[UUID, str, str], UUID] = {}
        self._audit: list[AuditEvent] = []

    def register(
        self,
        definition: CapabilityDefinition,
        input_model: type[BaseModel],
        handler: CapabilityHandler,
    ) -> None:
        with self._lock:
            if definition.name in self._capabilities:
                raise ValueError(f"capability already registered: {definition.name}")
            self._capabilities[definition.name] = (definition, input_model, handler)

    def get(
        self, name: str
    ) -> tuple[CapabilityDefinition, type[BaseModel], CapabilityHandler] | None:
        with self._lock:
            return self._capabilities.get(name)

    def list(self) -> tuple[CapabilityDefinition, ...]:
        with self._lock:
            registrations = sorted(
                self._capabilities.values(), key=lambda item: item[0].name
            )
            return tuple(item[0] for item in registrations)

    def execute_once(
        self,
        namespace: str,
        key: str,
        request_digest: str,
        operation: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        with self._lock:
            lookup = (namespace, key)
            existing = self._invocations.get(lookup)
            if existing:
                if existing[0] != request_digest:
                    raise IdempotencyConflictError("conflicting invocation")
                return existing[1], True
            result = operation()
            self._invocations[lookup] = (request_digest, result)
            return result, False

    def create_job(self, job: Job) -> tuple[Job, bool]:
        with self._lock:
            lookup = (job.household_id, job.kind, job.idempotency_key)
            existing_id = self._job_keys.get(lookup)
            if existing_id:
                existing = self._jobs[existing_id]
                if existing.input_digest != job.input_digest:
                    raise IdempotencyConflictError(
                        "idempotency key was already used with different job input"
                    )
                return existing, False
            self._jobs[job.id] = job
            self._job_keys[lookup] = job.id
            return job, True

    def get_job(self, job_id: UUID) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def transition_job(
        self,
        job_id: UUID,
        expected_version: int,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> Job:
        with self._lock:
            current = self._jobs[job_id]
            if current.version != expected_version:
                raise InvalidTransitionError(
                    f"stale job version: expected {expected_version}, current {current.version}"
                )
            updated = current.model_copy(
                update={
                    "status": status,
                    "version": current.version + 1,
                    "updated_at": utc_now(),
                    "result": result,
                    "error_code": error_code,
                }
            )
            self._jobs[job_id] = updated
            return updated

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock:
            expected_sequence = len(self._audit) + 1
            if event.sequence != expected_sequence:
                raise ValueError("audit sequence is not contiguous")
            expected_previous = self._audit[-1].event_hash if self._audit else "0" * 64
            if event.previous_hash != expected_previous:
                raise ValueError("audit hash chain is invalid")
            self._audit.append(event)

    def audit_events(self) -> tuple[AuditEvent, ...]:
        with self._lock:
            return tuple(self._audit)
