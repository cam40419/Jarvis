"""Shared project boards, scoped employee roles and human-controlled authority."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from simon.domain.errors import AuthenticationError, AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.native_agents import (
    AgentCredentialView,
    CreateNativeAgent,
    IssuedAgentCredential,
    IssueNativeAgentCredential,
    NativeActor,
    NativeAgent,
    NativeTeamPolicy,
    NativeTeamView,
    UpdateNativeAgent,
    UpdateNativeTeamPolicy,
)
from simon.domain.native_projects import (
    CreateNativeProject,
    CreateNativeTask,
    NativeProject,
    NativeProjectAccess,
    NativeProjectMember,
    NativeTask,
    PutNativeProjectMember,
    UpdateNativeProject,
    UpdateNativeTask,
    VersionedNativeCommand,
)
from simon.services.native_projects import NativeProjectService
from simon.services.native_teams import NativeTeamService


def native_projects_router(
    service: NativeProjectService,
    authenticate: Callable[[Request], ActorContext],
    teams: NativeTeamService,
) -> APIRouter:
    router = APIRouter(prefix="/v2/projects", tags=["native projects"])

    def scoped_actor(
        request: Request,
        x_workspace_id: Annotated[UUID | None, Header()] = None,
    ) -> NativeActor:
        actor: NativeActor
        authorization = request.headers.get("authorization")
        if authorization is not None:
            scheme, _, token = authorization.partition(" ")
            if (
                scheme.lower() != "bearer"
                or not token
                or any(
                    name in request.cookies for name in ("simon_session", "__Host-simon_session")
                )
            ):
                raise AuthenticationError("Use an agent bearer credential without a human session.")
            actor = service.agent_authority.resolve(token)
        else:
            actor = authenticate(request)
        if x_workspace_id is not None and x_workspace_id != actor.workspace_id:
            raise AuthorizationError("Workspace changed. Reload before continuing.")
        return actor

    @router.get("")
    def projects(
        actor: Annotated[NativeActor, Depends(scoped_actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(50, ge=1, le=100),
    ) -> tuple[NativeProject, ...]:
        return service.list_projects(actor, offset=offset, limit=limit)

    @router.post("", status_code=201)
    def create_project(
        body: CreateNativeProject,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeProject:
        return service.create_project(actor, body)

    @router.get("/{project_id}")
    def project(
        project_id: UUID,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeProject:
        return service.get_project(actor, project_id)

    @router.get("/{project_id}/access")
    def access(
        project_id: UUID,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
        candidates_offset: int = Query(0, ge=0, le=1_000_000),
        candidates_limit: int = Query(50, ge=1, le=100),
    ) -> NativeProjectAccess:
        return service.access(
            actor,
            project_id,
            candidates_offset=candidates_offset,
            candidates_limit=candidates_limit,
        )

    @router.put("/{project_id}")
    def update_project(
        project_id: UUID,
        body: UpdateNativeProject,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeProject:
        return service.update_project(actor, project_id, body)

    @router.get("/{project_id}/members")
    def members(
        project_id: UUID,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> tuple[NativeProjectMember, ...]:
        return service.members(actor, project_id)

    @router.put("/{project_id}/members")
    def put_member(
        project_id: UUID,
        body: PutNativeProjectMember,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeProjectMember:
        return service.put_member(actor, project_id, body)

    @router.post("/{project_id}/members/{actor_id}/remove")
    def remove_member(
        project_id: UUID,
        actor_id: UUID,
        body: VersionedNativeCommand,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeProject:
        return service.remove_member(actor, project_id, actor_id, body)

    @router.get("/{project_id}/tasks")
    def tasks(
        project_id: UUID,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(50, ge=1, le=100),
    ) -> tuple[NativeTask, ...]:
        return service.tasks(actor, project_id, offset=offset, limit=limit)

    @router.post("/{project_id}/tasks", status_code=201)
    def create_task(
        project_id: UUID,
        body: CreateNativeTask,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeTask:
        return service.create_task(actor, project_id, body)

    @router.get("/{project_id}/tasks/{task_id}")
    def task(
        project_id: UUID,
        task_id: UUID,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeTask:
        return service.get_task(actor, project_id, task_id)

    @router.put("/{project_id}/tasks/{task_id}")
    def update_task(
        project_id: UUID,
        task_id: UUID,
        body: UpdateNativeTask,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeTask:
        return service.update_task(actor, project_id, task_id, body)

    @router.post("/{project_id}/tasks/{task_id}/claim")
    def claim_task(
        project_id: UUID,
        task_id: UUID,
        body: VersionedNativeCommand,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeTask:
        return service.claim_task(actor, project_id, task_id, body)

    @router.get("/{project_id}/team")
    def team(
        project_id: UUID,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(100, ge=1, le=100),
    ) -> NativeTeamView:
        return teams.view(actor, project_id, offset, limit)

    @router.post("/{project_id}/agents", status_code=201)
    def create_agent(
        project_id: UUID,
        body: CreateNativeAgent,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeAgent:
        return teams.create(actor, project_id, body)

    @router.put("/{project_id}/agents/{agent_id}")
    def update_agent(
        project_id: UUID,
        agent_id: UUID,
        body: UpdateNativeAgent,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeAgent:
        return teams.update(actor, project_id, agent_id, body)

    @router.put("/{project_id}/team/policy")
    def update_team_policy(
        project_id: UUID,
        body: UpdateNativeTeamPolicy,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> NativeTeamPolicy:
        return teams.update_policy(actor, project_id, body)

    @router.get("/{project_id}/agents/{agent_id}/credentials")
    def credentials(
        project_id: UUID,
        agent_id: UUID,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> tuple[AgentCredentialView, ...]:
        return teams.credentials(actor, project_id, agent_id)

    @router.post("/{project_id}/agents/{agent_id}/credentials", status_code=201)
    def issue_credential(
        project_id: UUID,
        agent_id: UUID,
        body: IssueNativeAgentCredential,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> IssuedAgentCredential:
        return teams.issue_credential(actor, project_id, agent_id, body)

    @router.post("/{project_id}/agents/{agent_id}/credentials/{credential_id}/revoke")
    def revoke_credential(
        project_id: UUID,
        agent_id: UUID,
        credential_id: UUID,
        body: VersionedNativeCommand,
        actor: Annotated[NativeActor, Depends(scoped_actor)],
    ) -> AgentCredentialView:
        return teams.revoke_credential(actor, project_id, agent_id, credential_id, body)

    return router
