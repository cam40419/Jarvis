"""Native shared boards; these identifiers never address legacy project workers."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
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


def native_projects_router(
    service: NativeProjectService,
    authenticate: Callable[[Request], ActorContext],
) -> APIRouter:
    router = APIRouter(prefix="/v2/projects", tags=["native projects"])

    def scoped_actor(
        request: Request,
        x_workspace_id: Annotated[UUID | None, Header()] = None,
    ) -> ActorContext:
        actor = authenticate(request)
        if x_workspace_id is not None and x_workspace_id != actor.workspace_id:
            raise AuthorizationError("Workspace changed. Reload before continuing.")
        return actor

    @router.get("")
    def projects(
        actor: Annotated[ActorContext, Depends(scoped_actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(50, ge=1, le=100),
    ) -> tuple[NativeProject, ...]:
        return service.list_projects(actor, offset=offset, limit=limit)

    @router.post("", status_code=201)
    def create_project(
        body: CreateNativeProject,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> NativeProject:
        return service.create_project(actor, body)

    @router.get("/{project_id}")
    def project(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> NativeProject:
        return service.get_project(actor, project_id)

    @router.get("/{project_id}/access")
    def access(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
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
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> NativeProject:
        return service.update_project(actor, project_id, body)

    @router.get("/{project_id}/members")
    def members(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> tuple[NativeProjectMember, ...]:
        return service.members(actor, project_id)

    @router.put("/{project_id}/members")
    def put_member(
        project_id: UUID,
        body: PutNativeProjectMember,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> NativeProjectMember:
        return service.put_member(actor, project_id, body)

    @router.post("/{project_id}/members/{actor_id}/remove")
    def remove_member(
        project_id: UUID,
        actor_id: UUID,
        body: VersionedNativeCommand,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> NativeProject:
        return service.remove_member(actor, project_id, actor_id, body)

    @router.get("/{project_id}/tasks")
    def tasks(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(50, ge=1, le=100),
    ) -> tuple[NativeTask, ...]:
        return service.tasks(actor, project_id, offset=offset, limit=limit)

    @router.post("/{project_id}/tasks", status_code=201)
    def create_task(
        project_id: UUID,
        body: CreateNativeTask,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> NativeTask:
        return service.create_task(actor, project_id, body)

    @router.get("/{project_id}/tasks/{task_id}")
    def task(
        project_id: UUID,
        task_id: UUID,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> NativeTask:
        return service.get_task(actor, project_id, task_id)

    @router.put("/{project_id}/tasks/{task_id}")
    def update_task(
        project_id: UUID,
        task_id: UUID,
        body: UpdateNativeTask,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> NativeTask:
        return service.update_task(actor, project_id, task_id, body)

    @router.post("/{project_id}/tasks/{task_id}/claim")
    def claim_task(
        project_id: UUID,
        task_id: UUID,
        body: VersionedNativeCommand,
        actor: Annotated[ActorContext, Depends(scoped_actor)],
    ) -> NativeTask:
        return service.claim_task(actor, project_id, task_id, body)

    return router
