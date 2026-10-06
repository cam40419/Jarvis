from uuid import UUID

from pydantic import AwareDatetime, Field

from simon.domain.models import StrictModel, utc_now


class ManagedAccount(StrictModel):
    actor_id: UUID
    workspace_id: UUID
    invited_by: UUID
    display_name: str = Field(min_length=1, max_length=100)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    disabled: bool = False
    version: int = 0
    enrollment_hash: str | None = Field(default=None, repr=False)


class InviteAccount(StrictModel):
    display_name: str = Field(min_length=1, max_length=100, pattern=r"^\S(?:.*\S)?$")
    idempotency_key: UUID


class AccountAccess(StrictModel):
    enabled: bool = Field(strict=True)
    expected_version: int = Field(ge=0)
