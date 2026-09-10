from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel

from jarvis.domain.models import AuditEvent, CapabilityDefinition, Job, JobStatus

CapabilityHandler = Callable[[BaseModel], dict[str, Any]]


class CapabilityStore(Protocol):
    def register(
        self,
        definition: CapabilityDefinition,
        input_model: type[BaseModel],
        handler: CapabilityHandler,
    ) -> None: ...

    def get(
        self, name: str
    ) -> tuple[CapabilityDefinition, type[BaseModel], CapabilityHandler] | None: ...

    def list(self) -> Sequence[CapabilityDefinition]: ...


class InvocationStore(Protocol):
    def execute_once(
        self,
        namespace: str,
        key: str,
        request_digest: str,
        operation: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]: ...


class JobStore(Protocol):
    def create_job(self, job: Job) -> tuple[Job, bool]: ...

    def get_job(self, job_id: UUID) -> Job | None: ...

    def transition_job(
        self,
        job_id: UUID,
        expected_version: int,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> Job: ...


class AuditStore(Protocol):
    def append_audit(self, event: AuditEvent) -> None: ...

    def audit_events(self) -> Sequence[AuditEvent]: ...
