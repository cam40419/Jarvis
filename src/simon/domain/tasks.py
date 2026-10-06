"""Durable assistant work and research task contracts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from simon.domain.models import JobStatus, StrictModel


class CreateAssistantTask(StrictModel):
    title: str = Field(min_length=1, max_length=120, pattern=r"\S")
    instructions: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    task_type: Literal["work", "research"] = "work"
    project_id: UUID | None = None
    priority: int = Field(default=3, ge=1, le=5)
    idempotency_key: str = Field(min_length=8, max_length=200)


class EditAssistantTask(StrictModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=120, pattern=r"\S")
    instructions: str | None = Field(default=None, min_length=1, max_length=4000, pattern=r"\S")
    task_type: Literal["work", "research"] | None = None
    project_id: UUID | None = None
    change_project: bool = False
    priority: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def changed(self) -> "EditAssistantTask":
        if not any(
            (
                self.title is not None,
                self.instructions is not None,
                self.task_type is not None,
                self.change_project,
                self.priority is not None,
            )
        ):
            raise ValueError("provide at least one task change")
        return self


class ControlAssistantTask(StrictModel):
    action: Literal["pause", "resume", "cancel", "move_up", "move_down"]
    expected_version: int = Field(ge=1)


class SteerAssistantTask(StrictModel):
    message: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    expected_version: int = Field(ge=1)


class AssistantTask(StrictModel):
    id: UUID
    title: str
    instructions: str
    task_type: Literal["work", "research"]
    project_id: UUID | None = None
    project_name: str | None = None
    priority: int
    rank: int
    status: JobStatus
    phase: str
    progress: int = Field(ge=0, le=100)
    steering: tuple[str, ...] = ()
    version: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    thread_id: UUID | None = None
    run_id: UUID | None = None
    result: str | None = None
    error: str | None = None
    artifact_count: int = Field(default=0, ge=0)


class ProjectArtifact(StrictModel):
    id: UUID
    workspace_id: UUID
    actor_id: UUID
    project_id: UUID
    task_id: UUID
    name: str = Field(min_length=1, max_length=120)
    media_type: str = Field(min_length=1, max_length=100)
    byte_count: int = Field(ge=0, le=524_288)
    sha256: str = Field(min_length=64, max_length=64)
    created_at: AwareDatetime
