"""Bounded native execution, durable checkpoints and worker authority contracts."""

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from simon.domain.models import utc_now
from simon.domain.native_models import Fingerprint, Limit
from simon.domain.native_projects import NativeCommand, NativeModel, VersionedNativeCommand

ExecutionSlug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")]
LIVE_EXECUTION_STATUSES = frozenset({"queued", "running", "waiting", "unknown"})
EXECUTION_RUN_MUTABLE_FIELDS = frozenset(
    {
        "status",
        "version",
        "step_number",
        "attempt",
        "fence",
        "runner_id",
        "lease_token_hash",
        "lease_until",
        "next_wake_at",
        "result_text",
        "error_code",
        "updated_at",
        "finished_at",
        "result_task_version",
        "applied_step",
    }
)
EXECUTION_STEP_MUTABLE_FIELDS = frozenset(
    {"status", "usage_id", "result", "error_code", "version", "finished_at"}
)
EXECUTION_WAIT_MUTABLE_FIELDS = frozenset(
    {"status", "response", "resolved_by", "version", "resolved_at"}
)
EXECUTION_SCHEDULE_MUTABLE_FIELDS = frozenset(
    {
        "version",
        "definition_version",
        "enabled",
        "next_run_at",
        "interval_seconds",
        "timezone",
        "max_occurrences",
        "occurrence_count",
        "last_run_id",
        "last_occurrence_at",
        "updated_at",
    }
)
EXECUTION_RUNNER_MUTABLE_FIELDS = frozenset({"status", "version", "last_seen_at"})


def bounded_json(value: dict[str, Any], maximum: int) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > maximum:
            raise ValueError("Execution JSON exceeds its limit.")
        result: dict[str, Any] = json.loads(encoded)
        pending: list[Any] = [result]
        while pending:
            item = pending.pop()
            if isinstance(item, str) and "\x00" in item:
                raise ValueError("Execution JSON cannot contain NUL.")
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return result
    except (TypeError, RecursionError, UnicodeError) as exc:
        raise ValueError("Execution data must be bounded valid JSON.") from exc


class ExecutionRecord(NativeModel):
    @field_validator(
        "not_before",
        "deadline_at",
        "finished_at",
        "lease_until",
        "next_wake_at",
        "resolved_at",
        "next_run_at",
        "last_occurrence_at",
        "last_seen_at",
        check_fields=False,
    )
    @classmethod
    def execution_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class ExecutionPolicyFields(ExecutionRecord):
    enabled: bool = False
    auto_start: bool = False
    max_active_runs: int = Field(default=4, ge=1, le=32, strict=True)
    max_queued_runs: int = Field(default=100, ge=1, le=500, strict=True)
    max_steps: int = Field(default=8, ge=1, le=32, strict=True)
    max_attempts: int = Field(default=3, ge=1, le=8, strict=True)
    max_depth: int = Field(default=2, ge=0, le=4, strict=True)
    max_children: int = Field(default=8, ge=0, le=32, strict=True)
    max_model_calls: int = Field(default=16, ge=1, le=128, strict=True)
    max_cost_microusd: Limit = 0
    lease_seconds: int = Field(default=300, ge=180, le=600, strict=True)
    run_timeout_seconds: int = Field(default=86400, ge=300, le=604800, strict=True)


class NativeExecutionPolicy(ExecutionPolicyFields):
    workspace_id: UUID
    project_id: UUID
    version: int = Field(default=0, ge=0, strict=True)
    issued_by: UUID | None = None
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def persisted_issuer(self) -> Self:
        if self.version > 0 and self.issued_by is None:
            raise ValueError("Persisted execution authority requires an issuer.")
        return self


class WorkflowFields(ExecutionRecord):
    dependency_ids: tuple[UUID, ...] = Field(default=(), max_length=32)
    not_before: AwareDatetime | None = None

    @field_validator("dependency_ids")
    @classmethod
    def unique_dependencies(cls, value: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Dependencies must be unique.")
        return value


class NativeTaskWorkflow(WorkflowFields):
    workspace_id: UUID
    project_id: UUID
    task_id: UUID
    version: int = Field(default=0, ge=0, strict=True)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def no_self_dependency(self) -> Self:
        if self.task_id in self.dependency_ids:
            raise ValueError("A task cannot depend on itself.")
        return self


class NativeExecutionRun(ExecutionRecord):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    task_id: UUID
    agent_id: UUID
    issued_by: UUID
    root_run_id: UUID
    parent_run_id: UUID | None = None
    schedule_id: UUID | None = None
    schedule_definition_version: int | None = Field(default=None, ge=1, strict=True)
    engine: Literal["postgres"] = "postgres"
    definition_version: Literal[1] = 1
    task_version: int = Field(ge=1, strict=True)
    agent_version: int = Field(ge=1, strict=True)
    project_version: int = Field(ge=1, strict=True)
    workflow_version: int = Field(ge=0, strict=True)
    policy_version: int = Field(ge=1, strict=True)
    input_digest: Fingerprint
    context: dict[str, Any]
    bounds: NativeExecutionPolicy
    model_id: UUID
    depth: int = Field(default=0, ge=0, le=4, strict=True)
    status: Literal[
        "queued", "running", "waiting", "completed", "failed", "unknown", "cancelled", "stale"
    ] = "queued"
    version: int = Field(default=1, ge=1, strict=True)
    step_number: int = Field(default=0, ge=0, strict=True)
    applied_step: int = Field(default=0, ge=0, strict=True)
    attempt: int = Field(default=0, ge=0, strict=True)
    fence: int = Field(default=0, ge=0, strict=True)
    runner_id: UUID | None = None
    lease_token_hash: Fingerprint | None = Field(default=None, repr=False)
    lease_until: AwareDatetime | None = None
    next_wake_at: AwareDatetime | None = None
    result_text: str = Field(default="", max_length=24000)
    error_code: ExecutionSlug | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    deadline_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    result_task_version: int | None = Field(default=None, ge=1, strict=True)

    @field_validator("context")
    @classmethod
    def context_bound(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, 131072)

    @model_validator(mode="after")
    def run_identity(self) -> Self:
        if self.deadline_at <= self.created_at:
            raise ValueError("Execution deadline must follow creation.")
        if (self.parent_run_id is None) != (self.root_run_id == self.id):
            raise ValueError("Root executions identify themselves and have no parent.")
        if (self.parent_run_id is None) != (self.depth == 0) or self.parent_run_id == self.id:
            raise ValueError("Execution ancestry and depth do not agree.")
        if (self.schedule_id is None) != (self.schedule_definition_version is None):
            raise ValueError("Schedule authority requires its definition revision.")
        if (self.bounds.workspace_id, self.bounds.project_id, self.bounds.issued_by) != (
            self.workspace_id,
            self.project_id,
            self.issued_by,
        ) or self.bounds.version != self.policy_version:
            raise ValueError("Execution bounds must match the granting authority.")
        if self.depth > self.bounds.max_depth:
            raise ValueError("Execution exceeds its delegation depth.")
        if self.applied_step > self.step_number:
            raise ValueError("An applied checkpoint must exist in the execution trace.")
        return self


class NativeExecutionStep(ExecutionRecord):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    run_id: UUID
    root_run_id: UUID
    sequence: int = Field(ge=1, strict=True)
    fence: int = Field(ge=1, strict=True)
    operation_id: UUID
    kind: Literal["model", "reference"]
    status: Literal["prepared", "dispatched", "completed", "failed", "unknown"] = "prepared"
    usage_id: UUID | None = None
    request_digest: Fingerprint
    request: dict[str, Any] = Field(repr=False)
    result: dict[str, Any] = Field(default_factory=dict)
    error_code: ExecutionSlug | None = None
    version: int = Field(default=1, ge=1, strict=True)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    finished_at: AwareDatetime | None = None

    @field_validator("request")
    @classmethod
    def request_bound(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, 131072)

    @field_validator("result")
    @classmethod
    def result_bound(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, 32768)


class NativeExecutionEvent(ExecutionRecord):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1, strict=True)
    kind: ExecutionSlug
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("details")
    @classmethod
    def details_bound(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, 32768)


class NativeExecutionWait(ExecutionRecord):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    run_id: UUID
    correlation_id: UUID
    kind: Literal["human", "timer", "children"]
    question: str = Field(default="", max_length=4000)
    deadline_at: AwareDatetime
    status: Literal["pending", "resolved", "timed_out", "cancelled"] = "pending"
    response: str = Field(default="", max_length=8000)
    resolved_by: UUID | None = None
    version: int = Field(default=1, ge=1, strict=True)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    resolved_at: AwareDatetime | None = None


class NativeExecutionSignal(ExecutionRecord):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    run_id: UUID
    correlation_id: UUID
    received_by: UUID
    text: str = Field(min_length=1, max_length=8000)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ScheduleFields(ExecutionRecord):
    next_run_at: AwareDatetime | None = None
    interval_seconds: int | None = Field(default=None, ge=60, le=2592000, strict=True)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)
    max_occurrences: int = Field(default=100, ge=1, le=1000, strict=True)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Choose a valid IANA time zone.") from exc
        return value


class NativeExecutionSchedule(ScheduleFields):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    task_id: UUID
    issued_by: UUID
    version: int = Field(default=1, ge=1, strict=True)
    definition_version: int = Field(default=1, ge=1, strict=True)
    enabled: bool = True
    occurrence_count: int = Field(default=0, ge=0, le=1000, strict=True)
    last_run_id: UUID | None = None
    last_occurrence_at: AwareDatetime | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class NativeExecutionRunner(ExecutionRecord):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    name: str = Field(min_length=1, max_length=100)
    issued_by: UUID
    token_hash: Fingerprint = Field(repr=False)
    status: Literal["active", "revoked"] = "active"
    max_concurrent_runs: int = Field(default=1, ge=1, le=8, strict=True)
    version: int = Field(default=1, ge=1, strict=True)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime
    last_seen_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def expiration(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("Runner credential must expire after creation.")
        return self


class UpdateExecutionPolicy(ExecutionPolicyFields, NativeCommand):
    expected_version: int = Field(ge=0, strict=True)


class UpdateTaskWorkflow(WorkflowFields, NativeCommand):
    expected_version: int = Field(ge=0, strict=True)


class StartExecution(NativeCommand):
    expected_task_version: int = Field(ge=1, strict=True)
    agent_id: UUID | None = None


class ExecutionCommand(VersionedNativeCommand):
    pass


class SignalExecution(NativeCommand):
    correlation_id: UUID
    text: str = Field(min_length=1, max_length=8000)


class CreateExecutionSchedule(ScheduleFields, NativeCommand):
    expected_task_version: int = Field(ge=1, strict=True)
    next_run_at: AwareDatetime


class UpdateExecutionSchedule(ScheduleFields, VersionedNativeCommand):
    enabled: bool


class EnrollExecutionRunner(NativeCommand):
    name: str = Field(min_length=1, max_length=100)
    max_concurrent_runs: int = Field(default=1, ge=1, le=8, strict=True)
    ttl_seconds: int = Field(default=86400, ge=300, le=2592000, strict=True)


class RunnerLeaseCommand(NativeCommand):
    model_config = ConfigDict(hide_input_in_errors=True)
    run_id: UUID
    fence: int = Field(ge=1, strict=True)
    lease_token: str = Field(min_length=1, max_length=256, repr=False, exclude=True)
