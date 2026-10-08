"""Project team lifecycle; staffing never enlarges workspace or execution authority."""

import secrets
from datetime import timedelta
from uuid import UUID, uuid4

from simon.domain.errors import (
    AuthenticationError,
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.models import ActorContext, utc_now
from simon.domain.native_agents import (
    AgentCredentialView,
    CreateNativeAgent,
    IssuedAgentCredential,
    IssueNativeAgentCredential,
    NativeActor,
    NativeAgent,
    NativeAgentCredential,
    NativeTeamPolicy,
    NativeTeamView,
    ScopedAgentContext,
    UpdateNativeAgent,
    UpdateNativeTeamPolicy,
)
from simon.domain.native_projects import NativeProject, NativeTask, VersionedNativeCommand
from simon.domain.ports import Store
from simon.services.canonical import digest
from simon.services.identity import IDENTITY_LOCK
from simon.services.native_projects import NativeProjectService
from simon.services.scoped_agents import credential_hash


class NativeTeamService:
    def __init__(self, store: Store, projects: NativeProjectService) -> None:
        self.store = store
        self.projects = projects
        self.authority = projects.agent_authority

    def _policy(self, project: NativeProject) -> NativeTeamPolicy:
        return self.store.native_team_policy(project.workspace_id, project.id) or NativeTeamPolicy(
            workspace_id=project.workspace_id, project_id=project.id, updated_at=project.created_at
        )

    def _owner(self, actor: NativeActor, project_id: UUID) -> NativeProject:
        if isinstance(actor, ScopedAgentContext):
            raise AuthorizationError("Only a human project owner can change this authority.")
        return self.projects._project(actor, project_id, write=True, owner=True)

    def _manager(self, actor: NativeActor, project_id: UUID) -> NativeProject:
        if isinstance(actor, ScopedAgentContext):
            self.authority.check(actor, project_id, "team:manage")
            project = self.store.native_project(actor.workspace_id, project_id)
            assert project is not None
        else:
            project = self._owner(actor, project_id)
        self.projects._active(project)
        return project

    def _agent(self, actor: NativeActor, project_id: UUID, agent_id: UUID) -> NativeAgent:
        agent = self.store.native_agent(actor.workspace_id, project_id, agent_id)
        if agent is None:
            raise NotFoundError("Agent role not found.")
        return agent

    def _capacity(self, project: NativeProject) -> None:
        if (
            self.store.native_active_agent_count(project.workspace_id, project.id)
            >= self._policy(project).max_active_agents
        ):
            raise InvalidTransitionError("This project has reached its active agent limit.")

    def _audit(
        self,
        actor: NativeActor,
        event: str,
        project_id: UUID,
        resource_id: UUID,
        detail: dict[str, object],
    ) -> None:
        self.projects.audit.record(
            event_type="native." + event,
            actor=actor,
            resource_type="native_team",
            resource_id=str(resource_id),
            payload={
                "project_id": str(project_id),
                "actor_kind": "agent" if isinstance(actor, ScopedAgentContext) else "human",
                **(
                    {
                        "credential_id": str(actor.credential_id),
                        "agent_version": actor.agent_version,
                    }
                    if isinstance(actor, ScopedAgentContext)
                    else {}
                ),
                **detail,
            },
        )

    def view(
        self, actor: NativeActor, project_id: UUID, offset: int = 0, limit: int = 100
    ) -> NativeTeamView:
        self.projects._page(offset, limit)
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self.projects._project(actor, project_id)
            owner = False
            manager = False
            if isinstance(actor, ActorContext):
                try:
                    self._owner(actor, project_id)
                    owner = True
                except AuthorizationError:
                    pass
                manager = owner
            else:
                try:
                    self.authority.check(actor, project_id, "team:manage")
                    manager = True
                except AuthorizationError:
                    pass
            agents = self.store.native_agents(actor.workspace_id, project_id, offset, limit)
            active = project.status == "active"
            return NativeTeamView(
                project_id=project_id,
                policy=self._policy(project),
                agents=agents,
                agents_next_offset=offset + limit if len(agents) == limit else None,
                can_manage=manager and active,
                can_set_policy=owner and active,
                can_manage_credentials=owner,
                can_issue_credentials=owner and active,
            )

    def create(
        self, actor: NativeActor, project_id: UUID, command: CreateNativeAgent
    ) -> NativeAgent:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._manager(actor, project_id)
            if isinstance(actor, ScopedAgentContext) and command.can_manage_team:
                raise AuthorizationError("Agents cannot delegate team-management authority.")

            def create() -> NativeAgent:
                self._capacity(project)
                # Stable role keys make repeated staffing proposals reuse a role rather than
                # create a second employee with the same responsibility.
                for offset in range(0, 1_000_001, 100):
                    page = self.store.native_agents(actor.workspace_id, project_id, offset, 100)
                    if any(a.role_key == command.role_key for a in page):
                        raise InvalidTransitionError(
                            "This role key already exists. Reuse or revise that role."
                        )
                    if len(page) < 100:
                        break
                agent = NativeAgent(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    created_by=actor.actor_id if isinstance(actor, ActorContext) else None,
                    created_by_agent_id=actor.agent_id
                    if isinstance(actor, ScopedAgentContext)
                    else None,
                    **command.model_dump(exclude={"idempotency_key"}),
                )
                self.store.insert_native_agent(agent)
                self._audit(
                    actor,
                    "agent.created",
                    project_id,
                    agent.id,
                    {
                        "role_key": agent.role_key,
                        "version": agent.version,
                        "rationale": agent.rationale,
                        "can_manage_team": agent.can_manage_team,
                    },
                )
                return agent

            return self.projects._once(
                actor, f"project:{project_id}:agent", command, NativeAgent, create
            )

    def update(
        self, actor: NativeActor, project_id: UUID, agent_id: UUID, command: UpdateNativeAgent
    ) -> NativeAgent:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._manager(actor, project_id)
            agent = self._agent(actor, project_id, agent_id)
            if isinstance(actor, ScopedAgentContext) and (
                agent.created_by_agent_id != actor.agent_id
                or agent.can_manage_team
                or command.can_manage_team
                or agent.id == actor.agent_id
            ):
                raise AuthorizationError(
                    "Agents can revise only the subordinate roles they created."
                )

            def update() -> NativeAgent:
                self.projects._version(agent.version, command.expected_version)
                if command.status == "active" and agent.status != "active":
                    self._capacity(project)
                result = agent.model_copy(
                    update={
                        **command.model_dump(exclude={"idempotency_key", "expected_version"}),
                        "version": agent.version + 1,
                        "updated_at": utc_now(),
                    }
                )
                self.store.update_native_agent(result, command.expected_version)
                released: tuple[NativeTask, ...] = ()
                if result.status != "active":
                    released = self.store.native_release_agent_tasks(
                        actor.workspace_id, project_id, agent.id
                    )
                    for task in released:
                        self.projects._record(
                            actor, "task.agent_released", task, detail={"agent_id": str(agent.id)}
                        )
                self._audit(
                    actor,
                    "agent.updated",
                    project_id,
                    agent.id,
                    {
                        "version": result.version,
                        "status": result.status,
                        "can_manage_team": result.can_manage_team,
                        "released_tasks": len(released),
                        "rationale": result.rationale,
                    },
                )
                return result

            return self.projects._once(
                actor, f"project:{project_id}:agent:{agent_id}", command, NativeAgent, update
            )

    def update_policy(
        self, actor: NativeActor, project_id: UUID, command: UpdateNativeTeamPolicy
    ) -> NativeTeamPolicy:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._owner(actor, project_id)
            self.projects._active(project)

            def update() -> NativeTeamPolicy:
                policy = self._policy(project)
                self.projects._version(policy.version, command.expected_version)
                if command.max_active_agents < self.store.native_active_agent_count(
                    actor.workspace_id, project_id
                ):
                    raise InvalidTransitionError(
                        "Pause or retire agents before reducing the active limit."
                    )
                result = policy.model_copy(
                    update={
                        "max_active_agents": command.max_active_agents,
                        "agents_can_manage_team": command.agents_can_manage_team,
                        "version": policy.version + 1,
                        "updated_at": utc_now(),
                    }
                )
                self.store.save_native_team_policy(result, policy.version)
                self._audit(
                    actor,
                    "team.policy_updated",
                    project_id,
                    project_id,
                    {
                        "version": result.version,
                        "max_active_agents": result.max_active_agents,
                        "agents_can_manage_team": result.agents_can_manage_team,
                    },
                )
                return result

            return self.projects._once(
                actor, f"project:{project_id}:team-policy", command, NativeTeamPolicy, update
            )

    def _credential_view(self, credential: NativeAgentCredential) -> AgentCredentialView:
        valid = True
        try:
            self.authority.check(
                ScopedAgentContext(
                    workspace_id=credential.workspace_id,
                    project_id=credential.project_id,
                    agent_id=credential.agent_id,
                    credential_id=credential.id,
                    agent_version=credential.agent_version,
                    scopes=credential.scopes,
                ),
                credential.project_id,
            )
        except (AuthenticationError, AuthorizationError, NotFoundError):
            valid = False
        return AgentCredentialView.model_validate(
            {
                **credential.model_dump(exclude={"workspace_id", "project_id", "token_hash"}),
                "valid": valid,
            }
        )

    def credentials(
        self, actor: NativeActor, project_id: UUID, agent_id: UUID
    ) -> tuple[AgentCredentialView, ...]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._owner(actor, project_id)
            self._agent(actor, project_id, agent_id)
            return tuple(
                self._credential_view(c)
                for c in self.store.native_agent_credentials(
                    actor.workspace_id, project_id, agent_id
                )
            )

    def issue_credential(
        self,
        actor: NativeActor,
        project_id: UUID,
        agent_id: UUID,
        command: IssueNativeAgentCredential,
    ) -> IssuedAgentCredential:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._owner(actor, project_id)
            self.projects._active(project)
            agent = self._agent(actor, project_id, agent_id)
            if agent.status != "active":
                raise InvalidTransitionError("Activate this role before issuing worker access.")
            if "team:manage" in command.scopes and (
                not agent.can_manage_team or not self._policy(project).agents_can_manage_team
            ):
                raise AuthorizationError("Team management is not allowed for this role.")
            token: str | None = None

            def issue() -> dict[str, object]:
                nonlocal token
                self.projects._version(agent.version, command.expected_version)
                identifier = uuid4()
                token = f"sagent.{identifier}.{secrets.token_urlsafe(32)}"
                credential = NativeAgentCredential(
                    id=identifier,
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    agent_id=agent_id,
                    agent_version=agent.version,
                    token_hash=credential_hash(token),
                    scopes=command.scopes,
                    issued_by=actor.actor_id,
                    expires_at=utc_now() + timedelta(seconds=command.ttl_seconds),
                )
                self.store.insert_native_agent_credential(credential)
                self._audit(
                    actor,
                    "agent.credential_issued",
                    project_id,
                    identifier,
                    {
                        "agent_id": str(agent_id),
                        "agent_version": agent.version,
                        "scopes": sorted(command.scopes),
                        "expires_at": credential.expires_at.isoformat(),
                    },
                )
                # Receipt contains only metadata; retrying a lost response cannot recover a secret.
                return {"credential_id": str(identifier)}

            result, replayed = self.store.execute_once(
                f"native:{actor.workspace_id}:human:{actor.actor_id}:agent:{agent_id}:credential",
                command.idempotency_key,
                digest(command.model_dump(mode="json", exclude={"idempotency_key"})),
                issue,
            )
            credential = self.store.native_agent_credential(UUID(str(result["credential_id"])))
            assert credential is not None
            return IssuedAgentCredential(
                credential=self._credential_view(credential), token=token, replayed=replayed
            )

    def revoke_credential(
        self,
        actor: NativeActor,
        project_id: UUID,
        agent_id: UUID,
        credential_id: UUID,
        command: VersionedNativeCommand,
    ) -> AgentCredentialView:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._owner(actor, project_id)
            agent = self._agent(actor, project_id, agent_id)
            credential = self.store.native_agent_credential(credential_id)
            if credential is None or (
                credential.workspace_id,
                credential.project_id,
                credential.agent_id,
            ) != (actor.workspace_id, project_id, agent_id):
                raise NotFoundError("Worker credential not found.")

            def revoke() -> AgentCredentialView:
                self.projects._version(agent.version, command.expected_version)
                if credential.revoked_at is None:
                    self.store.revoke_native_agent_credential(
                        actor.workspace_id, project_id, agent_id, credential_id, utc_now()
                    )
                    self._audit(
                        actor,
                        "agent.credential_revoked",
                        project_id,
                        credential_id,
                        {"agent_id": str(agent_id)},
                    )
                current = self.store.native_agent_credential(credential_id)
                assert current is not None
                return self._credential_view(current)

            return self.projects._once(
                actor,
                f"project:{project_id}:credential:{credential_id}:revoke",
                command,
                AgentCredentialView,
                revoke,
            )
