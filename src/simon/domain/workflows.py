"""Versioned, bounded workflow contracts for scheduled work."""

import json
from typing import Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from simon.domain.home import HomeChange
from simon.domain.models import StrictModel, utc_now


class WorkflowStep(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,49}$")
    kind: Literal["action", "wait", "condition"] = "action"
    action: Literal["system.echo", "home.inventory", "home.set"] | None = None
    condition: Literal["printer.not_printing"] | None = None
    action_version: Literal[1] = 1
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = Field(default=(), max_length=25)
    delay_seconds: int = Field(default=0, ge=0, le=604800)
    not_before: AwareDatetime | None = None
    start_by: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_step(self) -> "WorkflowStep":
        if (self.kind == "action") != (self.action is not None):
            raise ValueError(
                "action steps require an action; waits and conditions cannot invoke actions"
            )
        if (self.kind == "condition") != (self.condition is not None):
            raise ValueError("condition steps require a condition")
        if self.action == "home.set":
            HomeChange.model_validate(self.inputs)
        elif self.action != "system.echo" and self.inputs:
            raise ValueError("this action does not accept inputs")
        if len(json.dumps(self.inputs, allow_nan=False).encode()) > 4096:
            raise ValueError("step inputs must fit in 4096 bytes")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate dependencies")
        if self.not_before and self.start_by and self.not_before >= self.start_by:
            raise ValueError("not_before must precede start_by")
        return self


class WorkflowTriggerSpec(StrictModel):
    kind: Literal["printer.print_started", "printer.print_finished", "device.state"]
    device_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    field: Literal[
        "online",
        "on",
        "brightness",
        "color",
        "watts",
        "energy_wh",
        "voltage",
        "current",
        "frequency",
        "temperature_c",
        "uptime_seconds",
    ] | None = None
    operator: Literal["equals", "above", "below"] = "equals"
    value: bool | float | str = True

    @model_validator(mode="after")
    def valid_predicate(self) -> "WorkflowTriggerSpec":
        if self.kind in {"printer.print_started", "printer.print_finished"}:
            if self.device_id is not None or self.field is not None:
                raise ValueError("printer triggers do not accept a device or field")
            if self.operator != "equals" or self.value is not True:
                raise ValueError("printer triggers use their built-in state transition")
            return self
        if self.device_id is None or self.field is None:
            raise ValueError("device triggers require a device and state field")
        if self.field in {"online", "on"}:
            if self.operator != "equals" or not isinstance(self.value, bool):
                raise ValueError("boolean device states use equals with true or false")
        elif self.field == "color":
            if (
                self.operator != "equals"
                or not isinstance(self.value, str)
                or len(self.value) != 7
                or not self.value.startswith("#")
                or any(character not in "0123456789abcdefABCDEF" for character in self.value[1:])
            ):
                raise ValueError("color states use equals with a six-digit hex color")
        elif isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError("numeric device states require a number")
        return self


class WorkflowSpec(StrictModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    steps: tuple[WorkflowStep, ...] = Field(min_length=1, max_length=25)
    trigger: WorkflowTriggerSpec | None = None

    @field_validator("trigger", mode="before")
    @classmethod
    def legacy_trigger(cls, value: Any) -> Any:
        if value == "printer.print_started":
            return {"kind": value}
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowSpec":
        ids = {s.id for s in self.steps}
        if len(ids) != len(self.steps):
            raise ValueError("step IDs must be unique")
        visited: set[str] = set()
        while len(visited) < len(ids):
            ready = {s.id for s in self.steps if set(s.depends_on) <= visited} - visited
            if not ready:
                raise ValueError("dependencies contain a cycle or unknown step")
            visited |= ready
        return self


class SaveWorkflow(StrictModel):
    spec: WorkflowSpec
    expected_version: int = Field(default=0, ge=0)
    idempotency_key: str = Field(min_length=8, max_length=200)


class WorkflowDefinition(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    actor_id: UUID
    version: int = Field(ge=1)
    spec: WorkflowSpec
    created_at: AwareDatetime = Field(default_factory=utc_now)


class StartWorkflow(StrictModel):
    definition_version: int = Field(ge=1)
    start_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    idempotency_key: str = Field(min_length=8, max_length=200)


class ControlWorkflow(StrictModel):
    action: Literal["pause", "resume", "cancel"]
    expected_version: int = Field(ge=1)


class SaveWorkflowSchedule(StrictModel):
    definition_version: int = Field(ge=1)
    frequency: Literal["daily", "weekly"]
    start_at: AwareDatetime
    time_zone: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=200)

    @field_validator("time_zone")
    @classmethod
    def valid_time_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("unknown IANA time zone") from error
        return value


class ControlWorkflowSchedule(StrictModel):
    action: Literal["pause", "resume"]
    expected_version: int = Field(ge=1)


class SaveWorkflowTrigger(StrictModel):
    definition_version: int = Field(ge=1)
    trigger: WorkflowTriggerSpec
    start_step_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,49}$")
    idempotency_key: str = Field(min_length=8, max_length=200)


class ControlWorkflowTrigger(StrictModel):
    action: Literal["pause", "resume"]
    expected_version: int = Field(ge=1)


class WorkflowTrigger(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    actor_id: UUID
    definition_id: UUID
    definition_version: int = Field(ge=1)
    name: str
    kind: Literal["printer.print_started", "printer.print_finished", "device.state"]
    device_id: str | None = None
    field: str | None = None
    operator: Literal["equals", "above", "below"] = "equals"
    value: bool | float | str = True
    start_step_id: str | None = None
    enabled: bool = True
    initialized: bool = False
    last_is_printing: bool | None = None
    last_observed_at: AwareDatetime | None = None
    next_check_at: AwareDatetime | None
    version: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class WorkflowSchedule(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    actor_id: UUID
    definition_id: UUID
    definition_version: int = Field(ge=1)
    name: str
    frequency: Literal["daily", "weekly"]
    start_at: AwareDatetime
    time_zone: str
    next_run_at: AwareDatetime | None
    enabled: bool = True
    version: int = Field(default=1, ge=1)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class StepAttempt(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    job_id: UUID
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    status: Literal["running", "succeeded", "failed", "interrupted"] = "running"


class WorkflowStepRun(StrictModel):
    step: WorkflowStep
    status: Literal["pending", "waiting", "running", "succeeded", "failed"] = "pending"
    due_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    attempts: tuple[StepAttempt, ...] = ()
    result: dict[str, Any] | None = None


class WorkflowRun(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    actor_id: UUID
    definition_id: UUID
    definition_version: int
    name: str
    steps: tuple[WorkflowStepRun, ...]
    status: Literal[
        "queued",
        "running",
        "waiting",
        "paused",
        "needs_attention",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    ] = "queued"
    version: int = 1
    created_at: AwareDatetime
    start_at: AwareDatetime
    expires_at: AwareDatetime
    next_wake_at: AwareDatetime | None
    lease_id: UUID | None = None
    lease_until: AwareDatetime | None = None
    worker_id: str | None = None
    error: str | None = None


class WorkflowEvent(StrictModel):
    run_id: UUID
    sequence: int
    type: str
    created_at: AwareDatetime
    step_id: str | None = None


class WorkerHeartbeat(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    seen_at: AwareDatetime


TERMINAL_WORKFLOWS = frozenset({"succeeded", "failed", "cancelled", "expired"})
