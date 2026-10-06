"""Reviewable setup suggestions, without executable grants or persistence commands."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from simon.domain.agent_profiles import AgentRoleDefinition
from simon.domain.errors import DomainError
from simon.domain.models import StrictModel

SetupMode = Literal["team", "member", "agent"]


class AgentSetupAssistantModelError(DomainError):
    """A safe setup-specific provider/configuration message for the API's 503 boundary."""

    code = "model_error"


class AgentSetupMessage(StrictModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Write a message about the agent or team you need")
        return value.strip()


class AgentSetupRole(AgentRoleDefinition):
    is_lead: bool
    rationale: str = Field(max_length=1000)


class AgentSetupDraft(StrictModel):
    team_name: str = Field(max_length=160)
    roles: tuple[AgentSetupRole, ...] = Field(max_length=8)

    def validate_mode(self, mode: SetupMode, *, allow_empty: bool = False) -> None:
        if not self.roles:
            if allow_empty:
                return
            raise ValueError("A proposal must contain at least one role")
        if mode != "team" and len(self.roles) != 1:
            raise ValueError("Agent and member proposals must contain exactly one role")
        if mode == "team" and (
            not self.team_name.strip() or sum(role.is_lead for role in self.roles) != 1
        ):
            raise ValueError("A team needs a name and exactly one lead")
        names = [role.name.casefold() for role in self.roles]
        if len(set(names)) != len(names):
            raise ValueError("Give each proposed role a distinct name")


class AgentSetupAssistantRequest(StrictModel):
    mode: SetupMode
    project_id: UUID | None = None
    messages: tuple[AgentSetupMessage, ...] = Field(min_length=1, max_length=16)
    current_draft: AgentSetupDraft | None = None
    privacy: Literal["local_only", "allow_cloud"] = "allow_cloud"

    @model_validator(mode="after")
    def bounded_conversation(self) -> Self:
        if sum(len(message.content) for message in self.messages) > 24000:
            raise ValueError("Keep setup conversation text within 24,000 characters")
        if self.messages[-1].role != "user":
            raise ValueError("The last setup message must come from the user")
        if self.current_draft is not None:
            self.current_draft.validate_mode(self.mode, allow_empty=True)
        return self


class AgentSetupAssistantResponse(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    proposal: AgentSetupDraft | None
    warnings: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...] = Field(max_length=32)

    @field_validator("message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A setup response needs a message")
        return value.strip()
