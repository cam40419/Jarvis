"""Authenticated project brief, decisions, and finding/history retrieval."""

from collections.abc import Callable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from simon.domain.models import ActorContext
from simon.domain.project_knowledge import (
    ActivityKind,
    ProjectKnowledge,
    ProjectKnowledgePage,
    UpdateProjectKnowledge,
)
from simon.domain.project_work import ProjectActivity
from simon.services.project_knowledge import ProjectKnowledgeService
from simon.services.project_work import ProjectWorkService


def project_knowledge_router(
    work: ProjectWorkService, authenticate: Callable[[Request], ActorContext],
) -> APIRouter:
    router = APIRouter(prefix="/knowledge", tags=["project knowledge"])
    knowledge = ProjectKnowledgeService(work)

    @router.get("")
    def get_knowledge(
        project_id: UUID, actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectKnowledge:
        return knowledge.get(actor, project_id)

    @router.patch("")
    def update_knowledge(
        project_id: UUID, body: UpdateProjectKnowledge,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectKnowledge:
        return knowledge.update(actor, project_id, body)

    @router.get("/history")
    def history(
        project_id: UUID, actor: Annotated[ActorContext, Depends(authenticate)],
        query: str = Query("", max_length=200), kind: ActivityKind | None = None,
        limit: int = Query(20, ge=1, le=50), cursor: str | None = Query(None, max_length=1024),
    ) -> ProjectKnowledgePage:
        return knowledge.history(actor, project_id, query=query, kind=kind,
                                 limit=limit, cursor=cursor)

    @router.get("/history/{activity_id}")
    def entry(
        project_id: UUID, activity_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectActivity:
        return knowledge.activity(actor, project_id, activity_id)

    return router
