"""Authenticated project output discovery and explicit local promotion."""

from collections.abc import Callable
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from simon.domain.artifacts import ArtifactPreview
from simon.domain.models import ActorContext
from simon.domain.project_outputs import ProjectOutput, ProjectOutputPage, PromoteProjectOutput
from simon.domain.run_journal import JournalKind, JournalPage, JournalRead
from simon.services.project_outputs import ProjectOutputService


def project_outputs_router(
    service: ProjectOutputService,
    authenticate: Callable[[Request], ActorContext],
) -> APIRouter:
    router = APIRouter(prefix="/v1/projects/{project_id}/outputs", tags=["project outputs"])

    @router.get("")
    def outputs(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        limit: int = Query(20, ge=1, le=50),
        cursor: str | None = Query(None, max_length=768),
        kind: Literal["response", "deliverable"] | None = None,
    ) -> ProjectOutputPage:
        return service.list(actor, project_id, limit=limit, cursor=cursor, kind=kind)

    @router.get("/journal/{run_id}")
    def journal_entries(
        project_id: UUID,
        run_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        offset: int = Query(0, ge=0, le=4096),
        limit: int = Query(30, ge=1, le=100),
        kind: JournalKind | None = None,
    ) -> JournalPage:
        service.authorize(actor, project_id)
        return service.journal.list(
            actor, project_id, run_id, offset=offset, limit=limit, kind=kind
        )

    @router.get("/journal/{run_id}/{entry_id}")
    def journal_read(
        project_id: UUID,
        run_id: UUID,
        entry_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        pointer: str = Query("", max_length=1000),
        offset: int = Query(0, ge=0, le=8388608),
        limit: int = Query(8000, ge=1, le=8000),
    ) -> JournalRead:
        service.authorize(actor, project_id)
        return service.journal.read(
            actor, project_id, run_id, entry_id, pointer=pointer, offset=offset, limit=limit
        )

    @router.get("/{run_id}/{artifact_id}/download")
    def download(
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> Response:
        item, content = service.read(actor, project_id, run_id, artifact_id)
        return Response(
            content,
            media_type=item.media_type,
            headers={
                "Content-Disposition": "attachment",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "private, no-store",
            },
        )

    @router.get("/{run_id}/{artifact_id}/preview")
    def preview(
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ArtifactPreview:
        _, artifact = service.source(actor, project_id, run_id, artifact_id)
        return service.artifacts.preview(artifact)

    @router.get("/{run_id}/{artifact_id}/document")
    def document(
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> Response:
        artifact, content = service.documents.materialize(actor, project_id, run_id, artifact_id)
        return Response(
            content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(artifact.name, safe=""),
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox",
                "Cache-Control": "private, no-store",
            },
        )

    @router.post("/{run_id}/{artifact_id}/promote")
    def promote(
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        body: PromoteProjectOutput,
        request: Request,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectOutput:
        return service.promote(
            actor,
            project_id,
            run_id,
            artifact_id,
            body,
            lambda: authenticate(request),
        )

    return router
