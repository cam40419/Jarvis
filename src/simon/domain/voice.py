from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field

from simon.domain.models import StrictModel, utc_now


class VoiceOffer(StrictModel):
    sdp: str = Field(min_length=10, max_length=64000, pattern=r"^v=0")
    idempotency_key: UUID
    timezone: str = Field(default="UTC", max_length=100, pattern=r"^[A-Za-z0-9_+./-]+$")


class VoiceFragment(StrictModel):
    event_id: str = Field(max_length=200)
    speaker: Literal["user", "assistant"]
    text: str = Field(max_length=8000)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class VoiceSession(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    household_id: UUID
    actor_id: UUID
    thread_id: UUID
    request_key: UUID
    provider_id: str | None = None
    state: Literal["starting", "active", "closing", "closed", "failed"] = "starting"
    created_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime
    seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    usage_final: bool = False
    fragments: tuple[VoiceFragment, ...] = Field(default=(), max_length=4000)
    backend_status: str = "Ready"
    error: str | None = None
