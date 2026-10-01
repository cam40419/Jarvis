"""Durable project reference knowledge and searchable activity contracts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from simon.domain.models import StrictModel
from simon.domain.project_work import ProjectActivity

ActivityKind = Literal[
    "finding", "progress", "note", "decision", "blocked", "cycle", "task", "configuration"
]


def valid_knowledge_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Project knowledge text cannot contain a null character")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("Project knowledge requires valid Unicode text") from error
    return value


class PinnedProjectDecision(StrictModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=160, pattern=r"\S")
    text: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    source_activity_id: UUID | None = None

    _storage_text = field_validator("title", "text")(valid_knowledge_text)


class ProjectKnowledgeContent(StrictModel):
    brief: str = Field(default="", max_length=8000)
    pinned_decisions: tuple[PinnedProjectDecision, ...] = Field(default=(), max_length=20)

    _storage_text = field_validator("brief")(valid_knowledge_text)

    @model_validator(mode="after")
    def unique_decisions(self) -> "ProjectKnowledgeContent":
        ids = [decision.id for decision in self.pinned_decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("Pinned decision IDs must be unique")
        return self


class UpdateProjectKnowledge(ProjectKnowledgeContent):
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=180)

    _storage_key = field_validator("idempotency_key")(valid_knowledge_text)


class ProjectKnowledge(ProjectKnowledgeContent):
    project_id: UUID
    version: int = Field(default=0, ge=0)
    updated_at: AwareDatetime | None = None


class ProjectKnowledgePage(StrictModel):
    project_id: UUID
    items: tuple[ProjectActivity, ...]
    next_cursor: str | None = None
