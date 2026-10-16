"""Workspace storage entitlements and immutable object-bound storage accounting."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, StringConstraints, field_validator, model_validator

from simon.domain.models import utc_now
from simon.domain.native_models import Fingerprint
from simon.domain.native_projects import NativeModel

DEFAULT_STORAGE_LIMIT_BYTES = 256 * 1024 * 1024
MAX_STORAGE_LIMIT_BYTES = 1024**4
StorageObjectKind = Literal[
    "source", "artifact", "delivery", "delivery_validation", "acceptance", "continuity"
]
StorageState = Literal["reserved", "retained", "released"]
StoragePlanKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class StorageEntitlement(NativeModel):
    workspace_id: UUID
    version: int = Field(ge=1, strict=True)
    plan_key: StoragePlanKey
    limit_bytes: int = Field(ge=0, le=MAX_STORAGE_LIMIT_BYTES, strict=True)
    override_limit_bytes: int | None = Field(
        default=None, ge=0, le=MAX_STORAGE_LIMIT_BYTES, strict=True
    )
    override_expires_at: AwareDatetime | None = None
    reason: str = Field(min_length=1, max_length=1000)
    issued_by: UUID
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("override_expires_at")
    @classmethod
    def override_utc(cls, value: datetime | None) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def paired_override(self) -> Self:
        if (self.override_limit_bytes is None) != (self.override_expires_at is None):
            raise ValueError("Storage overrides require both their limit and expiry.")
        return self

    def effective_limit(self, now: datetime) -> int:
        if (
            self.override_limit_bytes is not None
            and self.override_expires_at is not None
            and now < self.override_expires_at
        ):
            return self.override_limit_bytes
        return self.limit_bytes


class StorageCharge(NativeModel):
    workspace_id: UUID
    project_id: UUID
    object_kind: StorageObjectKind
    object_id: UUID
    bytes: int = Field(ge=0, strict=True)
    state: StorageState = "reserved"
    sha256: Fingerprint | None = None
    request_fingerprint: Fingerprint | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
