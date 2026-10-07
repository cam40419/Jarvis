"""Native shared project contracts, independent of memory records and tool bindings."""

from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from simon.domain.models import utc_now

ProjectRole = Literal["owner", "member"]
TaskStatus = Literal["todo", "in_progress", "in_review", "blocked", "done", "cancelled"]


class NativeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @field_validator("*")
    @classmethod
    def valid_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("Text cannot contain NUL characters.")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("Text must contain valid Unicode.") from exc
        return value


class TaskAssignment(NativeModel):
    kind: Literal["human", "pool"] = "pool"
    actor_id: UUID | None = None

    @model_validator(mode="after")
    def valid_target(self) -> Self:
        if (self.kind == "human") != (self.actor_id is not None):
            raise ValueError("Human assignments need an actor; pooled work has no assignee.")
        return self


class NativeProject(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=8000)
    status: Literal["active", "archived"] = "active"
    board_authority: Literal["native"] = "native"
    created_by: UUID
    version: int = Field(default=1, ge=1, strict=True)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class NativeProjectMember(NativeModel):
    workspace_id: UUID
    project_id: UUID
    actor_id: UUID
    role: ProjectRole = "member"
    created_at: AwareDatetime = Field(default_factory=utc_now)


class NativeTask(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8000)
    status: TaskStatus = "todo"
    assignment: TaskAssignment = Field(default_factory=TaskAssignment)
    created_by: UUID
    version: int = Field(default=1, ge=1, strict=True)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class NativeCommand(NativeModel):
    idempotency_key: str = Field(min_length=8, max_length=200)


class VersionedNativeCommand(NativeCommand):
    expected_version: int = Field(ge=1, strict=True)


class CreateNativeProject(NativeCommand):
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=8000)


class UpdateNativeProject(VersionedNativeCommand):
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=8000)
    status: Literal["active", "archived"] = "active"


class PutNativeProjectMember(VersionedNativeCommand):
    actor_id: UUID
    role: ProjectRole = "member"


class CreateNativeTask(NativeCommand):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8000)
    assignment: TaskAssignment = Field(default_factory=TaskAssignment)


class UpdateNativeTask(VersionedNativeCommand):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8000)
    status: TaskStatus
    assignment: TaskAssignment
