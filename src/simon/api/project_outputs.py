"""Authenticated project output discovery and explicit local promotion."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from simon.domain.models import ActorContext
from simon.domain.project_outputs import ProjectOutput, ProjectOutputPage, PromoteProjectOutput
from simon.services.project_outputs import ProjectOutputService


def project_outputs_router(
    service: ProjectOutputService,
    authenticate: Callable[[Request], ActorContext],
) -> APIRouter:
    router = APIRouter(prefix="/v1/projects/{project_id}/outputs", tags=["project outputs"])

    @router.get("")
    def outputs(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        limit: int = Query(20, ge=1, le=50),
        cursor: str | None = Query(None, max_length=768),
    ) -> ProjectOutputPage:
        return service.list(actor, project_id, limit=limit, cursor=cursor)

    @router.post("/{run_id}/{artifact_id}/promote")
    def promote(
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        body: PromoteProjectOutput,
        request: Request,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectOutput:
        return service.promote(
            actor,
            project_id,
            run_id,
            artifact_id,
            body,
            lambda: authenticate(request),
        )

    return router
