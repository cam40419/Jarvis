"""Human session access to the project decision inbox."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.services.native_review_inbox import NativeReviewInboxService


def native_review_inbox_router(
    service: NativeReviewInboxService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v2/projects/{project_id}/review-inbox", tags=["review inbox"])

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
        artifact_offset: int = Query(0, ge=0, le=1_000_000),
        packet_offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        return service.view(current, project_id, artifact_offset, packet_offset, limit)

    return router
