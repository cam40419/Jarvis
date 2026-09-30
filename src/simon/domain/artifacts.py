"""Immutable artifact references; local filesystem paths are never model inputs."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from simon.domain.errors import DomainError
from simon.domain.models import StrictModel


class ArtifactError(DomainError):
    code = "artifact_error"


class Artifact(StrictModel):
    id: UUID
    workspace_id: UUID
    actor_id: UUID
    run_id: UUID
    task_id: UUID
    name: str = Field(min_length=1, max_length=160)
    media_type: str = Field(pattern=r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$")
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or value != value.strip()
            or any(
                ord(character) < 32 or ord(character) == 127 or character in '/\\:<>|?*"'
                for character in value
            )
            or value.endswith(".")
        ):
            raise ValueError("Artifact names must be plain filenames")
        return value
