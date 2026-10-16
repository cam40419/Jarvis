"""Immutable same-project continuity evidence; a manifest grants no authority."""

import hashlib
import json
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, field_validator, model_validator

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_execution import bounded_json
from simon.domain.native_models import Fingerprint
from simon.domain.native_projects import NativeCommand, NativeModel

MAX_CONTINUITY_MANIFEST_BYTES = 1024 * 1024
MAX_CONTINUITY_EXPORT_BYTES = 32 * 1024 * 1024


class ContinuityContextChangedError(InvalidTransitionError):
    """Retain a core-authorized checkpoint until its handoff is explicitly refreshed."""


class ContinuityArtifactRef(NativeModel):
    artifact_id: UUID
    revision_id: UUID
    sha256: Fingerprint


class ContinuityRunRef(NativeModel):
    run_id: UUID
    version: int = Field(ge=1, strict=True)
    step_number: int = Field(ge=0, strict=True)
    applied_step: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def checkpoint_order(self) -> Self:
        if self.applied_step > self.step_number:
            raise ValueError("An applied checkpoint must exist in the captured execution.")
        return self


class NativeContinuityManifest(NativeModel):
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=240)
    project_version: int = Field(ge=1, strict=True)
    source_revision_ids: tuple[UUID, ...] = Field(default=(), max_length=2000)
    artifacts: tuple[ContinuityArtifactRef, ...] = Field(default=(), max_length=200)
    runs: tuple[ContinuityRunRef, ...] = Field(default=(), max_length=100)
    snapshot: dict[str, Any]
    limitations: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("snapshot")
    @classmethod
    def bounded_snapshot(cls, value: dict[str, Any]) -> dict[str, Any]:
        return bounded_json(value, MAX_CONTINUITY_MANIFEST_BYTES)

    @model_validator(mode="after")
    def exact_manifest(self) -> Self:
        for values in (
            self.source_revision_ids,
            tuple(ref.revision_id for ref in self.artifacts),
            tuple(ref.run_id for ref in self.runs),
        ):
            if len(set(values)) != len(values):
                raise ValueError("Continuity evidence references must be unique.")
        if len(continuity_manifest_bytes(self)) > MAX_CONTINUITY_MANIFEST_BYTES:
            raise ValueError("Continuity manifest exceeds its bounded byte size.")
        return self


def continuity_manifest_bytes(manifest: NativeContinuityManifest) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class NativeContinuityHandoff(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    created_by: UUID
    recipient_id: UUID
    manifest: NativeContinuityManifest
    manifest_sha256: Fingerprint
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def exact_hash(self) -> Self:
        if (
            hashlib.sha256(continuity_manifest_bytes(self.manifest)).hexdigest()
            != self.manifest_sha256
        ):
            raise ValueError("Handoff checksum must match its exact canonical manifest.")
        return self


class CreateContinuityHandoff(NativeCommand):
    recipient_id: UUID
    title: str = Field(default="Project handoff", min_length=1, max_length=240)


class ResumeContinuityHandoff(NativeCommand):
    manifest_sha256: Fingerprint
    run_id: UUID
    expected_run_version: int = Field(ge=1, strict=True)
    runner_id: UUID
    action: Literal["continue", "retry"] = "continue"
    runtime: Literal["native"] = "native"


class VerifyContinuityBundle(NativeModel):
    content_base64: str = Field(max_length=4 * ((MAX_CONTINUITY_EXPORT_BYTES + 2) // 3), repr=False)
