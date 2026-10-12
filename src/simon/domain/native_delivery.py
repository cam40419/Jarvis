"""Immutable delivery manifests bind generic files, previews and recorded evidence."""

import hashlib
import json
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from simon.domain.models import utc_now
from simon.domain.native_artifacts import (
    MAX_ARTIFACT_BYTES,
    NativeArtifactCheck,
    NativeArtifactDecision,
    NativeArtifactRevision,
)
from simon.domain.native_models import Fingerprint
from simon.domain.native_projects import NativeCommand, NativeModel

MAX_DELIVERY_MANIFEST_BYTES = 1024 * 1024
MAX_DELIVERY_PREVIEW_BYTES = 512 * 1024
DeliveryRole = Literal["source", "preview", "evidence", "attachment"]
DeliveryLimitation = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class DeliveryComponentInput(NativeModel):
    artifact_id: UUID
    revision_id: UUID
    sha256: Fingerprint
    role: DeliveryRole
    derived_from_revision_id: UUID | None = None

    @model_validator(mode="after")
    def preview_derivation(self) -> Self:
        if (self.role == "preview") != (self.derived_from_revision_id is not None):
            raise ValueError("Only preview components must identify their source revision.")
        if self.derived_from_revision_id == self.revision_id:
            raise ValueError("A preview cannot derive from itself.")
        return self


def validate_component_bindings(components: tuple[DeliveryComponentInput, ...]) -> None:
    if len({item.artifact_id for item in components}) != len(components) or len(
        {item.revision_id for item in components}
    ) != len(components):
        raise ValueError("Delivery components must identify distinct artifacts and revisions.")
    sources = {item.revision_id for item in components if item.role == "source"}
    if not sources:
        raise ValueError("A delivery packet must contain at least one source.")
    if any(
        item.role == "preview" and item.derived_from_revision_id not in sources
        for item in components
    ):
        raise ValueError("A delivery preview must derive from a source inside its packet.")


def validate_limitations(values: tuple[str, ...]) -> tuple[str, ...]:
    for value in values:
        NativeModel.valid_text(value)
    return values


class CreateDeliveryPacket(NativeCommand):
    title: str = Field(min_length=1, max_length=240)
    components: tuple[DeliveryComponentInput, ...] = Field(min_length=1, max_length=20)
    limitations: tuple[DeliveryLimitation, ...] = Field(default=(), max_length=20)
    previous_packet_id: UUID | None = None

    _safe_limitations = field_validator("limitations")(validate_limitations)

    @model_validator(mode="after")
    def component_bindings(self) -> Self:
        validate_component_bindings(self.components)
        return self


class DeliveryReviewState(NativeModel):
    id: UUID
    version: int = Field(ge=1, strict=True)
    status: Literal["running", "completed", "failed", "unknown", "stale", "cancelled"]
    verdict: Literal["approved", "changes_requested"] | None = None


class DeliveryComponent(DeliveryComponentInput):
    filename: str = Field(min_length=1, max_length=240)
    media_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES, strict=True)
    artifact_version: int = Field(ge=1, strict=True)
    task_version: int = Field(ge=1, strict=True)
    status_at_creation: str = Field(min_length=1, max_length=64)
    checks: tuple[NativeArtifactCheck, ...] = Field(default=(), max_length=100)
    decisions: tuple[NativeArtifactDecision, ...] = Field(default=(), max_length=100)
    review_ids: tuple[UUID, ...] = Field(default=(), max_length=100)
    review_states: tuple[DeliveryReviewState, ...] = Field(default=(), max_length=100)

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value: str) -> str:
        return NativeArtifactRevision.display_filename(value)

    @field_validator("media_type")
    @classmethod
    def valid_media_type(cls, value: str) -> str:
        return NativeArtifactRevision.simple_media_type(value)

    @model_validator(mode="after")
    def exact_evidence(self) -> Self:
        for values in (self.checks, self.decisions):
            if len({item.id for item in values}) != len(values):
                raise ValueError("Delivery evidence identities must be unique.")
            if any(
                item.revision_id != self.revision_id or item.sha256 != self.sha256
                for item in values
            ):
                raise ValueError("Delivery evidence must identify its component's exact content.")
        if any(item.artifact_id != self.artifact_id for item in self.decisions):
            raise ValueError("Delivery decisions must belong to their component artifact.")
        if len(set(self.review_ids)) != len(self.review_ids):
            raise ValueError("Delivery review identities must be unique.")
        if len({item.id for item in self.review_states}) != len(self.review_states) or {
            item.id for item in self.review_states
        } != set(self.review_ids):
            raise ValueError("Delivery review states must capture every referenced review exactly.")
        return self


class DeliveryPreview(NativeModel):
    revision_id: UUID
    sha256: Fingerprint
    path: str = Field(min_length=1, max_length=100)
    content_sha256: Fingerprint
    size_bytes: int = Field(ge=0, le=MAX_DELIVERY_PREVIEW_BYTES, strict=True)
    renderer: Literal["simon.safe-text.v1"] = "simon.safe-text.v1"
    supported: bool
    limitation: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def generated_preview(self) -> Self:
        if self.path != f"previews/{self.revision_id}.html":
            raise ValueError("Delivery preview paths must use their generated source identity.")
        if not self.supported and not self.limitation:
            raise ValueError("Unsupported previews require a clear limitation.")
        return self


class NativeDeliveryManifest(NativeModel):
    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=240)
    components: tuple[DeliveryComponent, ...] = Field(min_length=1, max_length=20)
    previews: tuple[DeliveryPreview, ...] = Field(default=(), max_length=20)
    limitations: tuple[DeliveryLimitation, ...] = Field(default=(), max_length=64)
    declared_limitations: tuple[DeliveryLimitation, ...] = Field(default=(), max_length=20)
    source_revision_ids: tuple[UUID, ...] = Field(default=(), max_length=1000)
    source_fingerprint: Fingerprint
    project_version: int = Field(ge=1, strict=True)
    policy_version: int = Field(ge=0, strict=True)

    _safe_limitations = field_validator("limitations", "declared_limitations")(validate_limitations)

    @model_validator(mode="after")
    def exact_bindings_and_size(self) -> Self:
        validate_component_bindings(self.components)
        sources = {
            item.revision_id: item.sha256 for item in self.components if item.role == "source"
        }
        if len({item.revision_id for item in self.previews}) != len(self.previews):
            raise ValueError("A source can have only one generated delivery preview.")
        if {item.revision_id for item in self.previews} != set(sources):
            raise ValueError("Every packet source requires its generated delivery preview.")
        if any(sources.get(item.revision_id) != item.sha256 for item in self.previews):
            raise ValueError("Generated previews must identify a packet source's exact content.")
        if len(set(self.source_revision_ids)) != len(self.source_revision_ids):
            raise ValueError("Intake source identities must be unique.")
        if len(manifest_bytes(self)) > MAX_DELIVERY_MANIFEST_BYTES:
            raise ValueError("Delivery manifest exceeds its bounded byte size.")
        return self


def manifest_bytes(manifest: NativeDeliveryManifest) -> bytes:
    """The portable checksum uses exact UTF-8 canonical JSON, independent of JSONB formatting."""
    return json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class NativeDeliveryPacket(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)
    previous_packet_id: UUID | None = None
    sequence: int = Field(ge=1, strict=True)
    manifest: NativeDeliveryManifest
    manifest_sha256: Fingerprint

    @model_validator(mode="after")
    def immutable_manifest_identity(self) -> Self:
        if (self.previous_packet_id is None) != (self.sequence == 1):
            raise ValueError("Only the first delivery packet has no previous packet.")
        if self.previous_packet_id == self.id:
            raise ValueError("A delivery packet cannot identify itself as its predecessor.")
        if hashlib.sha256(manifest_bytes(self.manifest)).hexdigest() != self.manifest_sha256:
            raise ValueError("Delivery manifest checksum must match its exact canonical bytes.")
        evidence: list[NativeArtifactCheck | NativeArtifactDecision] = []
        for component in self.manifest.components:
            evidence.extend(component.checks)
            evidence.extend(component.decisions)
        if any(
            (item.workspace_id, item.project_id) != (self.workspace_id, self.project_id)
            for item in evidence
        ):
            raise ValueError("Delivery evidence snapshots must belong to the packet's project.")
        return self
