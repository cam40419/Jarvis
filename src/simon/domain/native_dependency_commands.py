"""Owner commands for typed input authority and bounded, exact-head repairs."""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from simon.domain.native_dependencies import (
    DependencyBinding,
    DependencySelector,
    FactSourceBinding,
    bounded_snapshot,
)
from simon.domain.native_models import Fingerprint
from simon.domain.native_projects import NativeCommand, NativeModel


class UpsertNativeFact(NativeCommand):
    fact_id: UUID | None = None
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    kind: Literal["fact", "requirement"] = "fact"
    expected_version: int = Field(ge=0, strict=True)
    value_type: Literal["string", "number", "boolean", "json"]
    value: Any
    unit: str = Field(default="", max_length=64)
    sources: tuple[FactSourceBinding, ...] = Field(default=(), max_length=16)


class DeclareNativeDependencies(NativeCommand):
    expected_version: int = Field(ge=0, strict=True)
    expected_artifact_version: int = Field(ge=1, strict=True)
    revision_id: UUID
    sha256: Fingerprint
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    dependencies: tuple[DependencyBinding, ...] = Field(default=(), max_length=32)
    notes: str = Field(default="", max_length=2000)


class CreateImpactAssessment(NativeCommand):
    fact_id: UUID
    from_revision_id: UUID
    to_revision_id: UUID


class SelectorReplacement(NativeModel):
    selector: DependencySelector
    before: Any
    after: Any

    @field_validator("before", "after")
    @classmethod
    def finite_bounded_values(cls, value: Any) -> Any:
        return bounded_snapshot(value, 256 * 1024)


class RepairOutputEdit(NativeModel):
    artifact_id: UUID
    expected_version: int = Field(ge=1, strict=True)
    revision_id: UUID
    sha256: Fingerprint
    replacements: tuple[SelectorReplacement, ...] = Field(min_length=1, max_length=16)


class PreviewNativeRepair(NativeCommand):
    assessment_id: UUID
    edits: tuple[RepairOutputEdit, ...] = Field(min_length=1, max_length=10)
    rationale: str = Field(min_length=10, max_length=2000)

    @model_validator(mode="after")
    def distinct_outputs(self) -> "PreviewNativeRepair":
        if len({edit.artifact_id for edit in self.edits}) != len(self.edits):
            raise ValueError("A repair may edit each output only once.")
        return self


class CommitNativeRepair(NativeCommand):
    proposal_sha256: Fingerprint
