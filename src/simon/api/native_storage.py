"""Authenticated workspace storage visibility and installation-admin entitlements."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.storage_commands import UpdateStorageEntitlement
from simon.services.native_storage import NativeStorageService


def native_storage_router(
    service: NativeStorageService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(tags=["workspace storage"])

    def actor(
        request: Request, x_workspace_id: Annotated[UUID | None, Header()] = None
    ) -> ActorContext:
        current = authenticate(request)
        if x_workspace_id is not None and current.workspace_id != x_workspace_id:
            raise AuthorizationError("Workspace changed. Reload before continuing.")
        return current

    @router.get("/v2/projects/{project_id}/storage")
    def view(project_id: UUID, current: Annotated[ActorContext, Depends(actor)]) -> dict[str, Any]:
        return service.view(current, project_id)

    @router.post("/v2/projects/{project_id}/storage/entitlement")
    def update(
        project_id: UUID,
        body: UpdateStorageEntitlement,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.update(current, project_id, body)

    @router.get("/v2/projects/{project_id}/storage/operations/{key}")
    def operation(
        project_id: UUID, key: str, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.operation(current, project_id, key)

    @router.get("/v2/admin/workspaces/{workspace_id}/storage")
    def admin_view(
        workspace_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.admin_view(current, workspace_id)

    @router.post("/v2/admin/workspaces/{workspace_id}/storage/entitlement")
    def admin_update(
        workspace_id: UUID,
        body: UpdateStorageEntitlement,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.admin_update(current, workspace_id, body)

    @router.get("/v2/admin/workspaces/{workspace_id}/storage/operations/{key}")
    def admin_operation(
        workspace_id: UUID, key: str, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.admin_operation(current, workspace_id, key)

    return router
