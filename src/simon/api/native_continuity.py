"""Human-authenticated same-project handoff evidence and continuation."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.native_continuity import (
    CreateContinuityHandoff,
    ResumeContinuityHandoff,
    VerifyContinuityBundle,
)
from simon.services.native_continuity import NativeContinuityService


def native_continuity_router(
    service: NativeContinuityService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v2/projects/{project_id}/handoffs", tags=["project handoffs"])

    def actor(
        request: Request, x_workspace_id: Annotated[UUID | None, Header()] = None
    ) -> ActorContext:
        current = authenticate(request)
        if x_workspace_id is not None and current.workspace_id != x_workspace_id:
            raise AuthorizationError("Workspace changed. Reload before continuing.")
        return current

    @router.get("")
    def view(
        project_id: UUID,
        current: Annotated[ActorContext, Depends(actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        return service.view(current, project_id, offset, limit)

    @router.post("", status_code=201)
    def create(
        project_id: UUID,
        body: CreateContinuityHandoff,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.create(current, project_id, body)

    @router.get("/operations/{key}")
    def receipt(
        project_id: UUID, key: str, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.operation_receipt(current, project_id, key)

    @router.get("/{handoff_id}")
    def detail(
        project_id: UUID, handoff_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.detail(current, project_id, handoff_id)

    @router.get("/{handoff_id}/download")
    def download(
        project_id: UUID, handoff_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> Response:
        handoff, raw = service.download(current, project_id, handoff_id)
        return Response(
            raw,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="handoff-{handoff.id}.zip"',
                "X-Manifest-SHA256": handoff.manifest_sha256,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @router.post("/{handoff_id}/verify")
    def verify(
        project_id: UUID,
        handoff_id: UUID,
        body: VerifyContinuityBundle,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.verify(current, project_id, handoff_id, body)

    @router.post("/{handoff_id}/resume")
    def resume(
        project_id: UUID,
        handoff_id: UUID,
        body: ResumeContinuityHandoff,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.resume(current, project_id, handoff_id, body)

    return router
