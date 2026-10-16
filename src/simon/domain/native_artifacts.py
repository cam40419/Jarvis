"""Versioned native artifacts and exact-content review authority."""

import base64
import binascii
import hashlib
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from simon.domain.models import utc_now
from simon.domain.native_execution import bounded_json
from simon.domain.native_models import Fingerprint
from simon.domain.native_projects import NativeModel

MAX_ARTIFACT_BYTES = 1024 * 1024
ArtifactSlug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")]
ReviewFinding = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
ARTIFACT_MUTABLE_FIELDS = frozenset(
    {"name", "current_revision_id", "pending_repair_task_id", "version", "updated_at"}
)
ARTIFACT_REVIEW_MUTABLE_FIELDS = frozenset(
    {"status", "verdict", "summary", "findings", "usage_id", "finished_at", "version"}
)


class ArtifactRecord(NativeModel):
    @field_validator("finished_at", check_fields=False)
    @classmethod
    def finished_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


class ArtifactAuthor(ArtifactRecord):
    created_by: UUID | None = None
    created_by_agent_id: UUID | None = None

    @model_validator(mode="after")
    def one_author(self) -> Self:
        if (self.created_by is None) == (self.created_by_agent_id is None):
            raise ValueError("An artifact must have exactly one human or agent author.")
        return self


class NativeArtifact(ArtifactAuthor):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    task_id: UUID
    name: str = Field(min_length=1, max_length=240)
    kind: Literal["file"] = "file"
    current_revision_id: UUID | None = None
    pending_repair_task_id: UUID | None = None
    version: int = Field(default=1, ge=1, strict=True)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class NativeArtifactRevision(ArtifactAuthor):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    artifact_id: UUID
    task_id: UUID
    run_id: UUID | None = None
    sequence: int = Field(ge=1, strict=True)
    parent_revision_id: UUID | None = None
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(min_length=1, max_length=100)
    sha256: Fingerprint
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES, strict=True)
    content_base64: str = Field(max_length=4 * ((MAX_ARTIFACT_BYTES + 2) // 3), repr=False)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("filename")
    @classmethod
    def display_filename(cls, value: str) -> str:
        if any(character in value for character in ("/", "\\", "\r", "\n", ":")) or value in {
            ".",
            "..",
        }:
            raise ValueError("Filename must be a single display name.")
        return value

    @field_validator("media_type")
    @classmethod
    def simple_media_type(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+", value):
            raise ValueError("Media type must be a MIME type without parameters.")
        return value.lower()

    @model_validator(mode="after")
    def exact_content(self) -> Self:
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Artifact content must be valid base64.") from exc
        if base64.b64encode(content).decode("ascii") != self.content_base64:
            raise ValueError("Artifact base64 must use canonical encoding.")
        if len(content) != self.size_bytes or hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError("Artifact size and hash must match its exact content.")
        if (self.sequence == 1) != (self.parent_revision_id is None):
            raise ValueError("Only the first artifact revision has no parent.")
        if self.parent_revision_id == self.id:
            raise ValueError("An artifact revision cannot be its own parent.")
        return self


class NativeReviewPolicy(ArtifactRecord):
    workspace_id: UUID
    project_id: UUID
    version: int = Field(default=0, ge=0, strict=True)
    require_human_approval: bool = True
    require_agent_review: bool = True
    reviewer_agent_id: UUID | None = None
    max_repair_cycles: int = Field(default=3, ge=0, le=8, strict=True)
    max_pending_reviews: int = Field(default=20, ge=1, le=100, strict=True)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def acceptance_authority(self) -> Self:
        if not self.require_human_approval and not self.require_agent_review:
            raise ValueError(
                "Review policy must require human approval or independent agent review."
            )
        return self


class NativeArtifactCheck(ArtifactRecord):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    revision_id: UUID
    sha256: Fingerprint
    check_name: ArtifactSlug
    status: Literal["passed", "failed"]
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("details")
    @classmethod
    def bounded_details(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, 16384)


class NativeArtifactReview(ArtifactRecord):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    artifact_id: UUID
    revision_id: UUID
    sha256: Fingerprint
    artifact_version: int = Field(ge=1, strict=True)
    policy_version: int = Field(ge=0, strict=True)
    task_version: int = Field(ge=1, strict=True)
    project_version: int = Field(ge=1, strict=True)
    reviewer_agent_id: UUID
    reviewer_version: int = Field(ge=1, strict=True)
    review_task_id: UUID | None = None
    review_task_version: int | None = Field(default=None, ge=1, strict=True)
    model_id: UUID
    model_version: int = Field(ge=1, strict=True)
    endpoint_fingerprint: Fingerprint
    requested_by: UUID
    idempotency_key: str = Field(min_length=8, max_length=200)
    request_digest: Fingerprint
    usage_id: UUID | None = None
    status: Literal["running", "completed", "failed", "unknown", "stale", "cancelled"] = "running"
    verdict: Literal["approved", "changes_requested"] | None = None
    summary: str = Field(default="", max_length=4000)
    findings: tuple[ReviewFinding, ...] = Field(default=(), max_length=20)
    context: dict[str, Any] = Field(repr=False)
    version: int = Field(default=1, ge=1, strict=True)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    finished_at: AwareDatetime | None = None

    @field_validator("context")
    @classmethod
    def bounded_context(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, 131072)

    @field_validator("findings")
    @classmethod
    def valid_findings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            cls.valid_text(item)
        return value

    @model_validator(mode="after")
    def result_state(self) -> Self:
        if (self.review_task_id is None) != (self.review_task_version is None):
            raise ValueError("Review work items require their exact task version.")
        if (self.status == "completed") != (self.verdict is not None):
            raise ValueError("Only a completed review has a verdict.")
        if self.verdict == "approved" and self.findings:
            raise ValueError("Approved reviews cannot have unresolved findings.")
        if self.verdict == "changes_requested" and not self.findings:
            raise ValueError("Requested changes require review findings.")
        if (self.status == "running") != (self.finished_at is None):
            raise ValueError("Settled reviews require a finish time.")
        if self.finished_at is not None and self.finished_at < self.created_at:
            raise ValueError("Review cannot finish before it starts.")
        return self


class NativeArtifactDecision(ArtifactRecord):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    artifact_id: UUID
    revision_id: UUID
    sha256: Fingerprint
    policy_version: int = Field(ge=0, strict=True)
    task_version: int = Field(ge=1, strict=True)
    review_id: UUID | None = None
    repair_task_id: UUID | None = None
    decided_by: UUID
    automatic: bool = False
    authority_snapshot: dict[str, Any] = Field(default_factory=dict, repr=False)
    decision: Literal["approved", "changes_requested"]
    feedback: str = Field(default="", max_length=8000)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("authority_snapshot")
    @classmethod
    def bounded_authority(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, 131072)

    @model_validator(mode="after")
    def feedback_for_repair(self) -> Self:
        if self.decision == "changes_requested" and not self.feedback:
            raise ValueError("Requested changes require feedback.")
        if self.decision == "approved" and self.repair_task_id is not None:
            raise ValueError("Approval cannot create a repair task.")
        if self.automatic and (self.decision != "approved" or self.review_id is None):
            raise ValueError("Automatic acceptance requires an approving review.")
        return self
