from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from simon.domain.conversations import SubmitRun
from simon.domain.models import ActorContext
from simon.services.work_sessions import WorkSessionService


def session_router(
    service: WorkSessionService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v1/work-sessions", tags=["work sessions"])

    @router.get("")
    def listing(
        actor: Annotated[ActorContext, Depends(authenticate)],
        thread_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        return service.list(actor, thread_id)

    @router.post("/threads/{thread_id}", status_code=202)
    def submit(
        thread_id: UUID, body: SubmitRun, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.submit(actor, thread_id, body)

    @router.get("/{identifier}")
    def get(
        identifier: UUID, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.view(service.owned(actor, identifier))

    @router.post("/{identifier}/cancel")
    def cancel(
        identifier: UUID, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.cancel(actor, identifier)

    return router
