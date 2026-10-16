"""Human administration of finite, versioned workspace storage entitlements."""

from typing import Self

from pydantic import AwareDatetime, Field, model_validator

from simon.domain.native_projects import NativeCommand
from simon.domain.native_storage import MAX_STORAGE_LIMIT_BYTES, StoragePlanKey


class UpdateStorageEntitlement(NativeCommand):
    expected_version: int = Field(ge=0, strict=True)
    plan_key: StoragePlanKey
    limit_bytes: int = Field(ge=0, le=MAX_STORAGE_LIMIT_BYTES, strict=True)
    override_limit_bytes: int | None = Field(
        default=None, ge=0, le=MAX_STORAGE_LIMIT_BYTES, strict=True
    )
    override_expires_at: AwareDatetime | None = None
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def paired_override(self) -> Self:
        if (self.override_limit_bytes is None) != (self.override_expires_at is None):
            raise ValueError("An override requires both a byte limit and an expiry.")
        return self
