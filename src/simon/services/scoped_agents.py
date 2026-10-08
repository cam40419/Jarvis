"""Resolve machine credentials without granting human or workspace authority."""

import hashlib
import hmac
import re
from uuid import UUID

from simon.domain.errors import AuthenticationError, AuthorizationError, NotFoundError
from simon.domain.models import utc_now
from simon.domain.native_agents import AgentScope, NativeAgent, ScopedAgentContext
from simon.domain.ports import Store
from simon.services.identity import IDENTITY_LOCK, ROLE_SCOPES


def credential_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class NativeAgentAuthority:
    def __init__(self, store: Store) -> None:
        self.store = store

    def issuer_active(self, workspace_id: UUID, project_id: UUID, actor_id: UUID) -> bool:
        account = self.store.managed_account(actor_id)
        if account and account.disabled:
            return False
        membership = next(
            (m for m in self.store.memberships(actor_id) if m.workspace_id == workspace_id), None
        )
        if membership is None or "jobs:write" not in ROLE_SCOPES[membership.role]:
            return False
        member = self.store.native_project_member(workspace_id, project_id, actor_id)
        return membership.role == "owner" or (member is not None and member.role == "owner")

    def check(
        self, actor: ScopedAgentContext, project_id: UUID, scope: AgentScope = "board:read"
    ) -> NativeAgent:
        if actor.project_id != project_id:
            raise NotFoundError("Project not found.")
        credential = self.store.native_agent_credential(actor.credential_id)
        agent = self.store.native_agent(actor.workspace_id, project_id, actor.agent_id)
        project = self.store.native_project(actor.workspace_id, project_id)
        if (
            credential is None
            or credential.workspace_id != actor.workspace_id
            or credential.project_id != project_id
            or credential.agent_id != actor.agent_id
            or credential.revoked_at is not None
            or credential.expires_at <= utc_now()
            or credential.agent_version != actor.agent_version
            or actor.scopes != credential.scopes
            or agent is None
            or agent.status != "active"
            or agent.version != credential.agent_version
            or project is None
            or project.status != "active"
            or not self.issuer_active(actor.workspace_id, project_id, credential.issued_by)
        ):
            raise AuthenticationError("Agent access expired, changed or was revoked.")
        if scope not in credential.scopes:
            raise AuthorizationError("This agent credential does not allow that operation.")
        if scope == "team:manage":
            policy = self.store.native_team_policy(actor.workspace_id, project_id)
            if not agent.can_manage_team or (policy and not policy.agents_can_manage_team):
                raise AuthorizationError("Automatic team management is not allowed for this agent.")
        return agent

    def resolve(self, token: str) -> ScopedAgentContext:
        if not re.fullmatch(r"sagent\.[0-9a-f-]{36}\.[A-Za-z0-9_-]{43}", token):
            raise AuthenticationError("Invalid agent credential.")
        try:
            identifier = UUID(token.split(".")[1])
        except ValueError as exc:
            raise AuthenticationError("Invalid agent credential.") from exc
        with self.store.transaction(IDENTITY_LOCK):
            credential = self.store.native_agent_credential(identifier)
            if credential is None or not hmac.compare_digest(
                credential.token_hash, credential_hash(token)
            ):
                raise AuthenticationError("Invalid agent credential.")
            actor = ScopedAgentContext(
                workspace_id=credential.workspace_id,
                project_id=credential.project_id,
                agent_id=credential.agent_id,
                credential_id=credential.id,
                agent_version=credential.agent_version,
                scopes=credential.scopes,
            )
            with self.store.transaction(actor.workspace_id):
                self.check(actor, actor.project_id)
            return actor
