"""Human-only registered profile validation and explicit package approval routes."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.native_acceptance import (
    AcceptDeliveryPacket,
    AssignDeliveryProfile,
    ValidateDeliveryPacket,
)
from simon.services.native_acceptance import NativeAcceptanceService


def native_acceptance_router(
    service: NativeAcceptanceService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(
        prefix="/v2/projects/{project_id}/delivery-packets/{packet_id}/acceptance",
        tags=["package acceptance"],
    )

    def actor(
        request: Request, x_workspace_id: Annotated[UUID | None, Header()] = None
    ) -> ActorContext:
        current = authenticate(request)
        if x_workspace_id is not None and current.workspace_id != x_workspace_id:
            raise AuthorizationError("Workspace changed. Reload before continuing.")
        return current

    @router.get("")
    def detail(
        project_id: UUID, packet_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.detail(current, project_id, packet_id)

    @router.get("/operations/{key}")
    def receipt(
        project_id: UUID,
        packet_id: UUID,
        key: str,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.operation_receipt(current, project_id, packet_id, key)

    @router.put("/profile", status_code=201)
    def assign(
        project_id: UUID,
        packet_id: UUID,
        body: AssignDeliveryProfile,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.assign(current, project_id, packet_id, body)

    @router.post("/validate", status_code=201)
    def validate(
        project_id: UUID,
        packet_id: UUID,
        body: ValidateDeliveryPacket,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.validate(current, project_id, packet_id, body)

    @router.post("/decisions", status_code=201)
    def accept(
        project_id: UUID,
        packet_id: UUID,
        body: AcceptDeliveryPacket,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.accept(current, project_id, packet_id, body)

    return router
