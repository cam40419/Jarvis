from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskClass(StrEnum):
    READ = "read"
    WRITE_SOFT = "write_soft"
    WRITE_HARD = "write_hard"
    DANGEROUS = "dangerous"


class Channel(StrEnum):
    API = "api"
    CHAT = "chat"
    VOICE = "voice"
    SCHEDULER = "scheduler"
    WORKER = "worker"


class ActorContext(StrictModel):
    actor_id: UUID
    household_id: UUID
    channel: Channel
    scopes: frozenset[str] = frozenset()
    correlation_id: UUID = Field(default_factory=uuid4)


class CapabilityDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    version: int = Field(default=1, ge=1)
    description: str = Field(min_length=1, max_length=500)
    risk: RiskClass
    required_scopes: frozenset[str] = frozenset()
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    idempotent: bool = True


class CapabilityInvocation(StrictModel):
    capability: str
    arguments: dict[str, Any]
    idempotency_key: str = Field(min_length=8, max_length=200)
    confirmation_token: str | None = None


class CapabilityResult(StrictModel):
    invocation_id: UUID = Field(default_factory=uuid4)
    capability: str
    output: dict[str, Any]
    replayed: bool = False
    completed_at: datetime = Field(default_factory=utc_now)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    NEEDS_HUMAN = "needs_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class Job(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    created_by: UUID
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    schema_version: int = Field(default=1, ge=1)
    idempotency_key: str = Field(min_length=8, max_length=200)
    input: dict[str, Any]
    input_digest: str = Field(min_length=64, max_length=64)
    status: JobStatus = JobStatus.QUEUED
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    result: dict[str, Any] | None = None
    error_code: str | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value


class AuditEvent(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=1)
    occurred_at: datetime = Field(default_factory=utc_now)
    event_type: str
    actor_id: UUID
    household_id: UUID
    correlation_id: UUID
    resource_type: str
    resource_id: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class OutboxEvent(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    aggregate_type: str
    aggregate_id: str
    event_type: str
    schema_version: int = 1
    payload: dict[str, Any]
    correlation_id: UUID
    causation_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    published_at: datetime | None = None
    attempts: int = 0
