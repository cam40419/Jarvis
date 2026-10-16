"""Registered delivery profiles and immutable validation/owner acceptance evidence."""

from typing import Any, Literal, Self, cast
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, field_validator, model_validator

from simon.domain.models import utc_now
from simon.domain.native_dependencies import DependencySelector, bounded_snapshot, snapshot_hash
from simon.domain.native_models import Fingerprint
from simon.domain.native_projects import NativeCommand, NativeModel

PROFILE_NAME = "editable-text-json"
PROFILE_VERSION = 1
VALIDATOR_KEY = "simon.editable-text-json"
VALIDATOR_VERSION = 1
MAX_PACKAGE_RECORD_BYTES = 1024 * 1024
PackageRole = Literal["json_specification", "markdown_rationale", "schema_and_check_report"]


class PackageRoleBinding(NativeModel):
    role: PackageRole
    revision_id: UUID


class PackageEditProbe(NativeModel):
    selector: DependencySelector
    before: Any
    after: Any

    @field_validator("before", "after")
    @classmethod
    def bounded_value(cls, value: Any) -> Any:
        return bounded_snapshot(value, 8192)

    @model_validator(mode="after")
    def editable_field(self) -> Self:
        if self.selector.kind != "json_pointer" or not self.selector.path:
            raise ValueError("The edit probe must identify an existing JSON field.")
        return self


class AssignDeliveryProfile(NativeCommand):
    expected_version: int = Field(ge=0, strict=True)
    profile_name: str = Field(default=PROFILE_NAME, min_length=1, max_length=100)
    profile_version: int = Field(default=1, ge=1, strict=True)
    validator_version: int = Field(default=1, ge=1, strict=True)
    roles: tuple[PackageRoleBinding, ...] = Field(min_length=1, max_length=20)
    probe: PackageEditProbe


class ValidateDeliveryPacket(NativeCommand):
    assignment_id: UUID
    expected_profile_version: int = Field(ge=1, strict=True)
    manifest_sha256: Fingerprint
    validator_version: int = Field(default=1, ge=1, strict=True)


class AcceptDeliveryPacket(ValidateDeliveryPacket):
    validation_id: UUID
    authority_sha256: Fingerprint
    feedback: str = Field(default="", max_length=4000)


class PackageCheck(NativeModel):
    name: str = Field(min_length=1, max_length=100)
    status: Literal["passed", "failed"]
    message: str = Field(default="", max_length=2000)


class PackageRecord(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    packet_id: UUID
    manifest_sha256: Fingerprint
    created_by: UUID
    created_at: AwareDatetime = Field(default_factory=utc_now)


class NativeDeliveryProfileAssignment(PackageRecord):
    version: int = Field(ge=1, strict=True)
    profile_name: str = Field(min_length=1, max_length=100)
    profile_version: int = Field(ge=1, strict=True)
    profile_sha256: Fingerprint
    profile_snapshot: dict[str, Any]
    validator_key: str = Field(min_length=1, max_length=100)
    validator_version: int = Field(ge=1, strict=True)
    roles: tuple[PackageRoleBinding, ...] = Field(min_length=1, max_length=20)
    probe: PackageEditProbe

    @field_validator("profile_snapshot")
    @classmethod
    def bounded_profile(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], bounded_snapshot(value, 16 * 1024))

    @model_validator(mode="after")
    def exact_profile(self) -> Self:
        if self.profile_sha256 != snapshot_hash(self.profile_snapshot):
            raise ValueError("Profile checksum does not match its immutable definition.")
        return self


class NativeDeliveryValidation(PackageRecord):
    assignment_id: UUID
    profile_version: int = Field(ge=1, strict=True)
    profile_sha256: Fingerprint
    validator_key: str = Field(min_length=1, max_length=100)
    validator_version: int = Field(ge=1, strict=True)
    validator_sha256: Fingerprint
    validator_snapshot: dict[str, Any]
    passed: bool = Field(strict=True)
    checks: tuple[PackageCheck, ...] = Field(min_length=1, max_length=32)
    evidence: dict[str, Any]
    authority: dict[str, Any]
    authority_sha256: Fingerprint
    limitations: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("evidence", "authority")
    @classmethod
    def bounded_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], bounded_snapshot(value, 256 * 1024))

    @field_validator("validator_snapshot")
    @classmethod
    def bounded_validator(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], bounded_snapshot(value, 8192))

    @field_validator("limitations")
    @classmethod
    def bounded_limits(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            NativeModel.valid_text(value)
        if any(len(v) > 1000 for v in values):
            raise ValueError("Package limitations exceed their bound.")
        return values

    @model_validator(mode="after")
    def computed_results(self) -> Self:
        if self.validator_sha256 != snapshot_hash(self.validator_snapshot):
            raise ValueError("Validator checksum does not match its immutable definition.")
        if self.passed != all(c.status == "passed" for c in self.checks):
            raise ValueError("Validation status must match its check results.")
        if len({c.name for c in self.checks}) != len(self.checks):
            raise ValueError("Validation check names must be distinct.")
        if self.authority_sha256 != snapshot_hash(self.authority):
            raise ValueError("Validation authority checksum does not match.")
        return self


class NativeDeliveryAcceptance(PackageRecord):
    assignment_id: UUID
    profile_version: int = Field(ge=1, strict=True)
    validation_id: UUID
    authority_sha256: Fingerprint
    owner_authority: dict[str, Any]
    feedback: str = Field(default="", max_length=4000)
    decision: Literal["approved"] = "approved"

    @field_validator("owner_authority")
    @classmethod
    def bounded_owner(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], bounded_snapshot(value, 8192))
