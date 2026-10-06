"""Versioned project metadata; ownership, storage and execution settings are separate."""

from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from simon.domain.models import StrictModel
from simon.domain.project_knowledge import valid_knowledge_text


class ProjectDetailsEdit(StrictModel):
    expected_version: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"\S")
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("name", "description")
    @classmethod
    def storage_text(cls, value: str | None) -> str | None:
        return valid_knowledge_text(value) if value is not None else None

    @model_validator(mode="after")
    def has_edit(self) -> "ProjectDetailsEdit":
        if self.name is None and self.description is None:
            raise ValueError("Provide a project name or description to update")
        return self


class UpdateProjectDetails(ProjectDetailsEdit):
    idempotency_key: str = Field(min_length=8, max_length=180)

    _storage_key = field_validator("idempotency_key")(valid_knowledge_text)


class ProjectDetails(StrictModel):
    project_id: UUID
    name: str
    description: str
    version: int = Field(default=0, ge=0)
    updated_at: AwareDatetime | None = None
