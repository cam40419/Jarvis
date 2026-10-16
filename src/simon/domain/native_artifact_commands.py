"""Human commands bind review and acceptance to explicit current revisions."""

from typing import Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from simon.domain.native_artifacts import MAX_ARTIFACT_BYTES, NativeArtifactRevision
from simon.domain.native_models import Fingerprint
from simon.domain.native_projects import NativeCommand


class ArtifactCommand(NativeCommand):
    model_config = ConfigDict(hide_input_in_errors=True)


class UpdateArtifactReviewPolicy(ArtifactCommand):
    expected_version: int = Field(ge=0, strict=True)
    require_human_approval: bool = True
    require_agent_review: bool = True
    reviewer_agent_id: UUID | None = None
    max_repair_cycles: int = Field(default=3, ge=0, le=8, strict=True)
    max_pending_reviews: int = Field(default=20, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def automatic_acceptance_requires_review(self) -> Self:
        if not self.require_human_approval and not self.require_agent_review:
            raise ValueError("Automatic acceptance requires independent agent review.")
        return self


class UploadNativeArtifact(ArtifactCommand):
    artifact_id: UUID | None = None
    task_id: UUID
    expected_version: int = Field(ge=0, strict=True)
    expected_task_version: int = Field(ge=1, strict=True)
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(default="application/octet-stream", min_length=1, max_length=100)
    content_base64: str = Field(max_length=4 * ((MAX_ARTIFACT_BYTES + 2) // 3), repr=False)

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value: str) -> str:
        return NativeArtifactRevision.display_filename(value)

    @field_validator("media_type")
    @classmethod
    def valid_media_type(cls, value: str) -> str:
        return NativeArtifactRevision.simple_media_type(value)


class ExactArtifactCommand(ArtifactCommand):
    expected_version: int = Field(ge=1, strict=True)
    expected_policy_version: int = Field(ge=0, strict=True)
    expected_task_version: int = Field(ge=1, strict=True)
    revision_id: UUID
    sha256: Fingerprint


class ReviewNativeArtifact(ExactArtifactCommand):
    reviewer_agent_id: UUID | None = None


class DecideNativeArtifact(ExactArtifactCommand):
    decision: Literal["approved", "changes_requested"]
    feedback: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def feedback_required(self) -> Self:
        if self.decision == "changes_requested" and not self.feedback:
            raise ValueError("Describe the requested changes.")
        return self
