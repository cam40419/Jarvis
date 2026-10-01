"""Owner-attested file checks and versioned deliverable acceptance."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from simon.domain.artifacts import Artifact
from simon.domain.models import StrictModel


class FileCheck(StrictModel):
    format: str = Field(min_length=1, max_length=80, pattern=r"\S")
    procedure: str = Field(min_length=1, max_length=2000, pattern=r"\S")
    observed: str = Field(min_length=1, max_length=4000, pattern=r"\S")
    passed: bool


class RecordArtifactReview(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=100)
    artifact_id: UUID
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    delivery_key: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    inspected: Literal[True]
    verdict: Literal["passed", "changes_requested"]
    checks: tuple[FileCheck, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def passing_checks(self) -> "RecordArtifactReview":
        if self.verdict == "passed" and not all(check.passed for check in self.checks):
            raise ValueError("A passing review requires every recorded check to pass")
        return self


class ArtifactReview(StrictModel):
    id: UUID
    run_id: UUID
    plan_id: UUID
    project_id: UUID | None
    actor_id: UUID
    artifact: Artifact
    delivery_key: str
    evidence_source: Literal["owner_attestation"] = "owner_attestation"
    verdict: Literal["passed", "changes_requested"]
    checks: tuple[FileCheck, ...]
    created_at: datetime


class AcceptArtifactReview(StrictModel):
    expected_version: int = Field(ge=0)


class AcceptedRevision(StrictModel):
    review_id: UUID
    artifact: Artifact
    accepted_at: datetime


class ArtifactAcceptance(StrictModel):
    id: UUID
    delivery_key: str
    version: int = 0
    revisions: tuple[AcceptedRevision, ...] = ()
