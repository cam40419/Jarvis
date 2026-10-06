"""Durable project inbox and calendar contracts, separate from model execution."""

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from simon.domain.models import StrictModel


class QueueProjectRequest(StrictModel):
    instruction: str = Field(min_length=1, max_length=16000, pattern=r"\S")
    idempotency_key: str = Field(min_length=8, max_length=180)
    agent_id: str | None = Field(default=None, min_length=1, max_length=63)


class ReplyProjectWait(StrictModel):
    expected_version: int = Field(ge=1)
    message: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    idempotency_key: str = Field(min_length=8, max_length=180)


class CreateProjectWait(QueueProjectRequest):
    question: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    due_at: AwareDatetime | None = None


class ProjectScheduleSpec(StrictModel):
    name: str = Field(min_length=1, max_length=160, pattern=r"\S")
    instruction: str = Field(min_length=1, max_length=16000, pattern=r"\S")
    kind: Literal["once", "daily", "weekly"]
    timezone: str = Field(default="UTC", min_length=1, max_length=100)
    local_time: str = Field(default="09:00", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    weekdays: tuple[int, ...] = Field(default=(), max_length=7)
    run_at: AwareDatetime | None = None
    agent_id: str | None = Field(default=None, min_length=1, max_length=63)
    model_budget_usd: float = Field(gt=0, le=10000, allow_inf_nan=False)
    max_runs: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def valid_calendar(self) -> "ProjectScheduleSpec":
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Choose a valid IANA timezone") from None
        if len(set(self.weekdays)) != len(self.weekdays) or any(
            value < 0 or value > 6 for value in self.weekdays
        ):
            raise ValueError("Weekdays must be unique values from 0 (Monday) to 6 (Sunday)")
        if self.kind == "once" and self.run_at is None:
            raise ValueError("One-time schedules require an aware run_at timestamp")
        if self.kind != "once" and self.run_at is not None:
            raise ValueError("Recurring schedules use local_time, not run_at")
        if self.kind == "weekly" and not self.weekdays:
            raise ValueError("Weekly schedules require at least one weekday")
        if self.kind != "weekly" and self.weekdays:
            raise ValueError("Only weekly schedules use weekdays")
        return self


class CreateProjectSchedule(ProjectScheduleSpec):
    idempotency_key: str = Field(min_length=8, max_length=180)


class SetProjectSchedule(StrictModel):
    expected_version: int = Field(ge=1)
    enabled: bool


class ContinueProjectWork(StrictModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=180)


class CancelProjectRequest(StrictModel):
    expected_version: int = Field(ge=1)
