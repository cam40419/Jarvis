from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from jarvis.domain.models import StrictModel, utc_now

Role = Literal["owner", "member", "guest"]


class Membership(StrictModel):
    actor_id: UUID
    household_id: UUID
    role: Role
    display_name: str = "Jarvis user"
    household_name: str = "Household"


class Session(StrictModel):
    token_hash: str
    actor_id: UUID
    household_id: UUID
    method: Literal["passkey", "development"]
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime


class Enrollment(StrictModel):
    token_hash: str
    actor_id: UUID
    household_id: UUID
    expires_at: datetime


class Challenge(StrictModel):
    token_hash: str
    binding_hash: str
    challenge: str
    kind: Literal["registration", "authentication"]
    actor_id: UUID | None = None
    household_id: UUID | None = None
    enrollment_hash: str | None = None
    expires_at: datetime


class Passkey(StrictModel):
    credential_id: str
    actor_id: UUID
    public_key: str
    sign_count: int
    device_type: str
    backed_up: bool


DEV_ACTOR_ID = UUID("11111111-1111-4111-8111-111111111111")
DEV_HOUSEHOLD_ID = UUID("22222222-2222-4222-8222-222222222222")
