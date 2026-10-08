"""Persistent project roles and bounded, revocable machine authority."""

from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from simon.domain.models import ActorContext, utc_now
from simon.domain.native_projects import NativeCommand, NativeModel, VersionedNativeCommand

AgentScope = Literal["board:read", "board:write", "team:manage"]
AgentStatus = Literal["active", "paused", "retired"]


class AgentRoleFields(NativeModel):
    name: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1, max_length=8000)
    success_criteria: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(min_length=1, max_length=2000)
    can_manage_team: bool = False


class NativeAgent(AgentRoleFields):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    role_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    status: AgentStatus = "active"
    created_by: UUID | None = None
    created_by_agent_id: UUID | None = None
    version: int = Field(default=1, ge=1, strict=True)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def one_creator(self) -> Self:
        if (self.created_by is None) == (self.created_by_agent_id is None):
            raise ValueError("An agent must have exactly one human or agent creator.")
        return self


class NativeTeamPolicy(NativeModel):
    workspace_id: UUID
    project_id: UUID
    max_active_agents: int = Field(default=8, ge=1, le=100, strict=True)
    agents_can_manage_team: bool = True
    # Zero denotes an implicit default that has not yet been written.
    version: int = Field(default=0, ge=0, strict=True)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class NativeAgentCredential(NativeModel):
    id: UUID = Field(default_factory=uuid4)
    workspace_id: UUID
    project_id: UUID
    agent_id: UUID
    agent_version: int = Field(ge=1, strict=True)
    token_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: frozenset[AgentScope]
    issued_by: UUID
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ScopedAgentContext(NativeModel):
    workspace_id: UUID
    project_id: UUID
    agent_id: UUID
    credential_id: UUID
    agent_version: int
    scopes: frozenset[AgentScope]
    correlation_id: UUID = Field(default_factory=uuid4)

    @property
    def actor_id(self) -> UUID:
        return self.agent_id


NativeActor = ActorContext | ScopedAgentContext


class CreateNativeAgent(AgentRoleFields, NativeCommand):
    role_key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")


class UpdateNativeAgent(AgentRoleFields, VersionedNativeCommand):
    status: AgentStatus


class UpdateNativeTeamPolicy(NativeCommand):
    max_active_agents: int = Field(ge=1, le=100, strict=True)
    agents_can_manage_team: bool
    expected_version: int = Field(ge=0, strict=True)


class IssueNativeAgentCredential(VersionedNativeCommand):
    scopes: frozenset[AgentScope] = frozenset({"board:read", "board:write"})
    ttl_seconds: int = Field(default=900, ge=60, le=3600, strict=True)

    @model_validator(mode="after")
    def readable(self) -> Self:
        if "board:read" not in self.scopes:
            raise ValueError("Agent credentials must include board:read.")
        return self


class AgentCredentialView(NativeModel):
    id: UUID
    agent_id: UUID
    agent_version: int
    scopes: frozenset[AgentScope]
    issued_by: UUID
    expires_at: AwareDatetime
    revoked_at: AwareDatetime | None
    created_at: AwareDatetime
    valid: bool


class IssuedAgentCredential(NativeModel):
    credential: AgentCredentialView
    # Secret is returned once, never persisted in receipts or exposed on GET.
    token: str | None
    replayed: bool


class NativeTeamView(NativeModel):
    project_id: UUID
    policy: NativeTeamPolicy
    agents: tuple[NativeAgent, ...]
    agents_next_offset: int | None = None
    can_manage: bool
    can_set_policy: bool
    can_manage_credentials: bool
    can_issue_credentials: bool
