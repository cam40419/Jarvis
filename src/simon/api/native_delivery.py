"""Human-authenticated delivery manifests and exact portable file bundles."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.native_delivery import CreateDeliveryPacket
from simon.services.native_delivery import NativeDeliveryService


def native_delivery_router(
    service: NativeDeliveryService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(
        prefix="/v2/projects/{project_id}/delivery-packets", tags=["delivery packets"]
    )

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
        artifact_offset: int = Query(0, ge=0, le=1_000_000),
        artifact_limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        return service.view(current, project_id, offset, limit, artifact_offset, artifact_limit)

    @router.post("", status_code=201)
    def create(
        project_id: UUID,
        body: CreateDeliveryPacket,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.create(current, project_id, body)

    @router.get("/operations/{key}")
    def receipt(
        project_id: UUID, key: str, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.operation_receipt(current, project_id, key)

    @router.get("/{packet_id}")
    def detail(
        project_id: UUID, packet_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.detail(current, project_id, packet_id)

    @router.get("/{packet_id}/download")
    def download(
        project_id: UUID, packet_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> Response:
        packet, content = service.download(current, project_id, packet_id)
        return Response(
            content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="delivery-{packet.id}.zip"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "X-Manifest-SHA256": packet.manifest_sha256,
            },
        )

    return router
