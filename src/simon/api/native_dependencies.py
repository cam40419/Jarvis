"""Authenticated typed facts, exact output dependencies and atomic repair commands."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.native_dependency_commands import (
    CommitNativeRepair,
    CreateImpactAssessment,
    DeclareNativeDependencies,
    PreviewNativeRepair,
    UpsertNativeFact,
)
from simon.services.native_dependencies import NativeDependencyService


def native_dependencies_router(
    service: NativeDependencyService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(
        prefix="/v2/projects/{project_id}/dependencies", tags=["facts and dependencies"]
    )

    def actor(
        request: Request, x_workspace_id: Annotated[UUID | None, Header()] = None
    ) -> ActorContext:
        current = authenticate(request)
        if x_workspace_id is not None and current.workspace_id != x_workspace_id:
            raise AuthorizationError("Workspace changed. Reload before continuing.")
        return current

    @router.get("")
    def view(project_id: UUID, current: Annotated[ActorContext, Depends(actor)]) -> dict[str, Any]:
        return service.view(current, project_id)

    @router.get("/operations/{key}")
    def receipt(
        project_id: UUID, key: str, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.operation_receipt(current, project_id, key)

    @router.post("/facts", status_code=201)
    def save_fact(
        project_id: UUID,
        body: UpsertNativeFact,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.save_fact(current, project_id, body)

    @router.get("/facts/{fact_id}")
    def fact(
        project_id: UUID, fact_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.fact_detail(current, project_id, fact_id)

    @router.post("/outputs/{artifact_id}", status_code=201)
    def declare(
        project_id: UUID,
        artifact_id: UUID,
        body: DeclareNativeDependencies,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.declare(current, project_id, artifact_id, body)

    @router.post("/assessments", status_code=201)
    def assess(
        project_id: UUID,
        body: CreateImpactAssessment,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.assess(current, project_id, body)

    @router.get("/assessments/{assessment_id}")
    def assessment(
        project_id: UUID, assessment_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.assessment_detail(current, project_id, assessment_id)

    @router.post("/repairs", status_code=201)
    def preview(
        project_id: UUID,
        body: PreviewNativeRepair,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.preview_repair(current, project_id, body)

    @router.get("/repairs/{proposal_id}")
    def repair(
        project_id: UUID, proposal_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.repair_detail(current, project_id, proposal_id)

    @router.post("/repairs/{proposal_id}/commit")
    def commit(
        project_id: UUID,
        proposal_id: UUID,
        body: CommitNativeRepair,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.commit_repair(current, project_id, proposal_id, body)

    return router
