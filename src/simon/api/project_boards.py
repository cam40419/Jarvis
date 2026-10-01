"""Authenticated management of explicitly granted project board connections."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from simon.domain.models import ActorContext, StrictModel
from simon.domain.project_board_state import (
    BindProjectBoard,
    ImportBoardTasks,
    ProjectBoardState,
    PublishBoardTasks,
    ReconcileBoardOperation,
)
from simon.domain.project_boards import BoardList, BoardTaskPage
from simon.services.project_boards import ProjectBoardService


class SyncProjectBoard(StrictModel):
    expected_version: int = Field(ge=1)


def board_blockers(
    service: ProjectBoardService, actor: ActorContext, state: ProjectBoardState,
) -> tuple[str, ...]:
    reasons = list(state.blocked_reasons)
    if state.binding is not None:
        connection = next((item for item in service.connections(actor)
                           if item["id"] == state.binding.connection_id), None)
        if connection is None:
            reasons.append("The saved board connection is no longer available to this account.")
        else:
            reasons.extend(connection["blocked_reasons"])
            if state.binding.list_id not in connection["list_ids"]:
                reasons.append("The saved project list is no longer authorized.")
    return tuple(dict.fromkeys(reasons))


def project_boards_router(
    service: ProjectBoardService, authenticate: Callable[[Request], ActorContext],
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["managed project boards"])

    def snapshot(actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        state = service.get(actor, project_id)
        return {
            "state": state.model_dump(mode="json"),
            "operations": [
                item.model_dump(mode="json", include={
                    "id", "kind", "todo_id", "state", "marker", "remote_id",
                    "error", "created_at", "updated_at",
                }) for item in service.operations(actor, project_id)
            ],
            "blocked_reasons": board_blockers(service, actor, state),
        }

    @router.get("/project-boards/connections")
    def connections(
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> list[dict[str, Any]]:
        return service.connections(actor)

    @router.get("/project-boards/connections/{connection_id}/boards")
    def boards(
        connection_id: str, actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> tuple[BoardList, ...]:
        return service.boards(actor, connection_id)

    @router.get("/projects/{project_id}/board")
    def get(
        project_id: UUID, actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return snapshot(actor, project_id)

    @router.patch("/projects/{project_id}/board")
    def bind(
        project_id: UUID, body: BindProjectBoard,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        service.bind(actor, project_id, body)
        return snapshot(actor, project_id)

    @router.get("/projects/{project_id}/board/preview")
    def preview(
        project_id: UUID, actor: Annotated[ActorContext, Depends(authenticate)],
        page: Annotated[int, Query(ge=0, le=1000)] = 0,
    ) -> BoardTaskPage:
        return service.preview(actor, project_id, page=page)

    @router.post("/projects/{project_id}/board/import")
    def import_tasks(
        project_id: UUID, body: ImportBoardTasks,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        service.import_tasks(actor, project_id, body)
        return snapshot(actor, project_id)

    @router.post("/projects/{project_id}/board/publish")
    def publish(
        project_id: UUID, body: PublishBoardTasks,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        service.publish(actor, project_id, body)
        return snapshot(actor, project_id)

    @router.post("/projects/{project_id}/board/sync")
    def sync(
        project_id: UUID, body: SyncProjectBoard,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        service.sync(actor, project_id, expected_version=body.expected_version)
        return snapshot(actor, project_id)

    @router.post("/projects/{project_id}/board/reconcile")
    def reconcile(
        project_id: UUID, body: ReconcileBoardOperation,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        service.reconcile(actor, project_id, body)
        return snapshot(actor, project_id)

    return router
