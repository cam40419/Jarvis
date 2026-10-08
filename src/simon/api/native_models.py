"""Human-owned project model enrollment, qualification and resource policies."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.native_models import (
    EnrollProjectModel,
    ModelResourcePolicy,
    ProbeProjectModel,
    ReconcileModelUsage,
    UpdateModelResourcePolicy,
    UpdateProjectModel,
)
from simon.services.model_usage import ModelUsageService
from simon.services.project_models import ProjectModelService


def native_models_router(
    service: ProjectModelService,
    usage: ModelUsageService,
    authenticate: Callable[[Request], ActorContext],
) -> APIRouter:
    router = APIRouter(prefix="/v2/projects/{project_id}/models", tags=["project models"])

    def actor(
        request: Request,
        x_workspace_id: Annotated[UUID | None, Header()] = None,
    ) -> ActorContext:
        current = authenticate(request)
        if x_workspace_id is not None and current.workspace_id != x_workspace_id:
            raise AuthorizationError("Workspace changed. Reload before continuing.")
        return current

    @router.get("")
    def view(project_id: UUID, current: Annotated[ActorContext, Depends(actor)]) -> dict[str, Any]:
        return service.view(current, project_id)

    @router.post("/connections", status_code=201)
    def enroll(
        project_id: UUID,
        body: EnrollProjectModel,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.enroll(current, project_id, body)

    @router.put("/connections/{model_id}")
    def update(
        project_id: UUID,
        model_id: UUID,
        body: UpdateProjectModel,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.update(current, project_id, model_id, body)

    @router.post("/connections/{model_id}/probe")
    def probe(
        project_id: UUID,
        model_id: UUID,
        body: ProbeProjectModel,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.probe(current, project_id, model_id, body)

    @router.get("/operations/{key}")
    def operation(
        project_id: UUID,
        key: str,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.operation_receipt(current, project_id, key)

    @router.put("/policy")
    def policy(
        project_id: UUID,
        body: UpdateModelResourcePolicy,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> ModelResourcePolicy:
        return usage.update_policy(current, project_id, body)

    @router.put("/workspace-policy")
    def workspace_policy(
        project_id: UUID,
        body: UpdateModelResourcePolicy,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> ModelResourcePolicy:
        return usage.update_policy(current, project_id, body, workspace=True)

    @router.get("/usage")
    def history(
        project_id: UUID,
        current: Annotated[ActorContext, Depends(actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        return usage.list_usage(current, project_id, offset=offset, limit=limit)

    @router.post("/usage/{usage_id}/reconcile")
    def reconcile(
        project_id: UUID,
        usage_id: UUID,
        body: ReconcileModelUsage,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return usage.reconcile(current, project_id, usage_id, body).model_dump(
            mode="json", exclude={"endpoint_snapshot"}
        )

    return router
