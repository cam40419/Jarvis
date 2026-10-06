"""Versioned project records, reusable procedures and private composer drafts."""

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from simon.domain.models import StrictModel
from simon.domain.project_knowledge import valid_knowledge_text

RecordKind = Literal["supplier", "product", "quote", "contact", "decision", "procedure", "note"]
RecordConfidence = Literal["unverified", "supported", "owner_confirmed"]


class ProjectRecordSource(StrictModel):
    label: str = Field(min_length=1, max_length=240, pattern=r"\S")
    url: str | None = Field(default=None, max_length=2048)
    activity_id: UUID | None = None

    _text = field_validator("label")(valid_knowledge_text)

    @field_validator("url")
    @classmethod
    def source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        valid_knowledge_text(value)
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or "\\" in value
            or any(ord(char) < 33 for char in value)
        ):
            raise ValueError("Source links must be public HTTP(S) URLs without credentials")
        return value

    @model_validator(mode="after")
    def reference(self) -> "ProjectRecordSource":
        if (self.url is None) == (self.activity_id is None):
            raise ValueError("Provide one source URL or saved project activity ID")
        return self


class ProjectRecordContent(StrictModel):
    kind: RecordKind = "note"
    title: str = Field(min_length=1, max_length=240, pattern=r"\S")
    summary: str = Field(default="", max_length=8000)
    fields: dict[str, str] = Field(default_factory=dict, max_length=30)
    sources: tuple[ProjectRecordSource, ...] = Field(default=(), max_length=20)
    confidence: RecordConfidence = "unverified"
    status: Literal["active", "archived"] = "active"
    checked_at: AwareDatetime | None = None
    review_after: AwareDatetime | None = None
    steps: tuple[str, ...] = Field(default=(), max_length=30)
    success_checks: tuple[str, ...] = Field(default=(), max_length=20)

    _text = field_validator("title", "summary")(valid_knowledge_text)

    @field_validator("fields")
    @classmethod
    def valid_fields(cls, value: dict[str, str]) -> dict[str, str]:
        for key, text in value.items():
            if not key.strip() or len(key) > 80 or len(text) > 2000:
                raise ValueError(
                    "Record fields need a name up to 80 and value up to 2000 characters"
                )
            valid_knowledge_text(key)
            valid_knowledge_text(text)
        return value

    @field_validator("steps", "success_checks")
    @classmethod
    def procedure_text(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for text in value:
            if not text.strip() or len(text) > 2000:
                raise ValueError("Procedure entries must contain 1 to 2000 characters")
            valid_knowledge_text(text)
        return value

    @model_validator(mode="after")
    def record_contract(self) -> "ProjectRecordContent":
        if self.kind == "procedure" and (not self.steps or not self.success_checks):
            raise ValueError("Procedures require steps and completion checks")
        if self.kind != "procedure" and (self.steps or self.success_checks):
            raise ValueError("Only procedures can contain steps and completion checks")
        if self.confidence == "supported" and not self.sources:
            raise ValueError("Supported records require source references")
        if self.review_after and self.checked_at and self.review_after < self.checked_at:
            raise ValueError("Review date cannot precede the checked date")
        return self


class UpdateProjectRecord(ProjectRecordContent):
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=180)

    _key = field_validator("idempotency_key")(valid_knowledge_text)


class ProjectRecord(ProjectRecordContent):
    id: UUID
    project_id: UUID
    version: int = Field(ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    updated_by: UUID
    run_id: UUID | None = None
    plan_id: UUID | None = None
    agent_id: str | None = None


class SaveProjectDraft(StrictModel):
    text: str = Field(default="", max_length=16000)
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=180)

    _text = field_validator("text", "idempotency_key")(valid_knowledge_text)


class ProjectDraft(StrictModel):
    project_id: UUID
    key: str
    text: str = ""
    version: int = Field(default=0, ge=0)
    updated_at: AwareDatetime | None = None
