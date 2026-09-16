from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from simon.domain.connected_tools import ActionProposal, WebSource
from simon.domain.home import HomeCommand
from simon.domain.model import AnswerLength, ProfileName
from simon.domain.models import StrictModel, utc_now


class ResponsePreferences(StrictModel):
    profile: ProfileName = "auto"
    answer_length: AnswerLength | Literal["auto"] = "auto"
    auto_deep_enabled: bool = True
    version: int = Field(default=0, ge=0)


class SavePreferences(StrictModel):
    profile: ProfileName
    answer_length: AnswerLength | Literal["auto"]
    auto_deep_enabled: bool
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=200)


Rating = Literal["helpful", "too_slow", "needs_depth"]


class RunFeedback(StrictModel):
    run_id: UUID
    rating: Rating | None = None
    version: int = Field(default=0, ge=0)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class SaveFeedback(StrictModel):
    rating: Rating | None
    expected_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=200)


class AnswerReference(StrictModel):
    run_id: UUID
    output_message_id: UUID
    feedback: RunFeedback | None = None
    web_sources: tuple[WebSource, ...] = ()
    actions: tuple[ActionProposal, ...] = ()
    home_commands: tuple[HomeCommand, ...] = ()
