from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from simon.domain.models import ActorContext
from simon.domain.tasks import (
    AssistantTask,
    ControlAssistantTask,
    CreateAssistantTask,
    EditAssistantTask,
    ProjectArtifact,
    SteerAssistantTask,
)
from simon.services.tasks import AssistantTaskService


def task_router(
    service: AssistantTaskService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v1/assistant-tasks", tags=["assistant tasks"])

    @router.get("")
    def tasks(
        actor: Annotated[ActorContext, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> tuple[AssistantTask, ...]:
        return service.list(actor, offset, limit)

    @router.post("", status_code=202)
    def create(
        body: CreateAssistantTask,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> AssistantTask:
        return service.create(actor, body)

    @router.get("/{identifier}")
    def get(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> AssistantTask:
        return service.get(actor, identifier)

    @router.patch("/{identifier}")
    def edit(
        identifier: UUID,
        body: EditAssistantTask,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> AssistantTask:
        return service.edit(actor, identifier, body)

    @router.post("/{identifier}/control")
    def control(
        identifier: UUID,
        body: ControlAssistantTask,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> AssistantTask:
        return service.control(actor, identifier, body)

    @router.post("/{identifier}/steer")
    def steer(
        identifier: UUID,
        body: SteerAssistantTask,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> AssistantTask:
        return service.steer(actor, identifier, body)

    @router.get("/{identifier}/artifacts")
    def task_artifacts(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> tuple[ProjectArtifact, ...]:
        return service.task_artifacts(actor, identifier)

    @router.get("/projects/{project_id}/artifacts")
    def project_artifacts(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> tuple[ProjectArtifact, ...]:
        return service.artifacts(actor, project_id)

    @router.get("/artifacts/{identifier}/download")
    def download_artifact(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> Response:
        artifact, content = service.artifact(actor, identifier)
        return Response(
            content=content,
            media_type=artifact.media_type,
            headers={"Content-Disposition": f'attachment; filename="{artifact.name}"'},
        )

    return router
