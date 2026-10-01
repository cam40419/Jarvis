"""User-authored roles select server-owned skills, never raw execution grants."""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from simon.domain.agent_platform import AgentProfile
from simon.domain.models import StrictModel


class AgentRoleDefinition(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=4000)
    skill_ids: tuple[str, ...] = Field(min_length=1, max_length=128)

    @field_validator("name", "description")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Use nonempty text")
        return value.strip()

    @field_validator("skill_ids")
    @classmethod
    def unique_skills(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value) or any(not item or len(item) > 256 for item in value):
            raise ValueError("Select distinct skill identifiers")
        return tuple(sorted(value))


class CreateAgentProfile(AgentRoleDefinition):
    idempotency_key: str = Field(min_length=8, max_length=180)

    @field_validator("idempotency_key")
    @classmethod
    def bounded_key(cls, value: str) -> str:
        if len(value.strip()) < 8:
            raise ValueError("Use an idempotency key of at least eight non-padding characters")
        return value.strip()


class UpdateAgentProfile(CreateAgentProfile):
    expected_version: int = Field(ge=1)


class AgentSkill(StrictModel):
    id: str
    name: str
    description: str
    category: str = "General"
    tool_ids: tuple[str, ...] = ()
    source_agent_ids: tuple[str, ...] = ()
    state: Literal[
        "configured",
        "disabled",
        "unconfigured",
        "unavailable",
        "permission_required",
    ] = "configured"
    blocked_reasons: tuple[str, ...] = ()


class AgentProfileRecord(StrictModel):
    id: str
    name: str
    description: str
    skill_ids: tuple[str, ...]
    version: int = Field(ge=1)
    profile: AgentProfile
    editable: Literal[True] = True
    state: Literal["configured", "blocked"] = "configured"
    blocked_reasons: tuple[str, ...] = ()
    skills: tuple[AgentSkill, ...]
    created_at: datetime
    updated_at: datetime
