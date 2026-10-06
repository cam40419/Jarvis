"""Authenticated autosaved drafts, structured records, procedures and context views."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from simon.domain.models import ActorContext
from simon.domain.project_storage import ProjectStorageRead, SaveProjectStorage
from simon.domain.project_workspace import (
    ProjectDraft,
    ProjectRecord,
    RecordKind,
    SaveProjectDraft,
    UpdateProjectRecord,
)
from simon.services.project_output_replication import ProjectOutputReplicationService
from simon.services.project_storage import ProjectStorageService
from simon.services.project_workspace import ProjectWorkspaceService


def project_workspace_router(
    service: ProjectWorkspaceService,
    authenticate: Callable[[Request], ActorContext],
    *,
    replication: ProjectOutputReplicationService | None = None,
    locations: ProjectStorageService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/projects/{project_id}/workspace", tags=["project workspace"])

    @router.get("/drafts/{key}")
    def draft(
        project_id: UUID,
        key: str,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectDraft:
        return service.draft(actor, project_id, key)

    @router.put("/drafts/{key}")
    def save_draft(
        project_id: UUID,
        key: str,
        body: SaveProjectDraft,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectDraft:
        return service.save_draft(actor, project_id, key, body)

    @router.get("/records")
    def records(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        query: str = Query("", max_length=200),
        kind: RecordKind | None = None,
        offset: int = Query(0, ge=0, le=1000000),
        limit: int = Query(20, ge=1, le=50),
        include_archived: bool = False,
    ) -> dict[str, Any]:
        return service.records(
            actor,
            project_id,
            query=query,
            kind=kind,
            offset=offset,
            limit=limit,
            include_archived=include_archived,
        )

    @router.get("/records/{record_id}")
    def record(
        project_id: UUID,
        record_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectRecord:
        return service.record(actor, project_id, record_id)

    @router.put("/records/{record_id}")
    def save_record(
        project_id: UUID,
        record_id: UUID,
        body: UpdateProjectRecord,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectRecord:
        return service.save_record(actor, project_id, record_id, body)

    @router.get("/records/{record_id}/revisions")
    def revisions(
        project_id: UUID,
        record_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        before_version: int | None = Query(None, ge=1),
        limit: int = Query(20, ge=1, le=50),
    ) -> dict[str, Any]:
        return service.revisions(
            actor, project_id, record_id, before_version=before_version, limit=limit
        )

    @router.get("/briefing")
    def briefing(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.briefing(actor, project_id)

    @router.get("/storage")
    def storage(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        service.authorize(actor, project_id)
        return (
            replication.state(actor, project_id)
            if replication
            else {"local_storage": "authoritative", "cloud_enabled": False}
        )

    if locations:

        @router.get("/file-locations")
        def file_locations(
            project_id: UUID, actor: Annotated[ActorContext, Depends(authenticate)]
        ) -> dict[str, Any]:
            return {**locations.snapshot(actor, project_id), **locations.options(actor, project_id)}

        @router.put("/file-locations")
        def save_file_locations(
            project_id: UUID,
            body: SaveProjectStorage,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> dict[str, Any]:
            return locations.save(actor, project_id, body)

        @router.post("/file-locations/read")
        def read_location(
            project_id: UUID,
            body: ProjectStorageRead,
            request: Request,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> dict[str, Any]:
            return locations.execute(
                actor,
                project_id,
                body.location_id,
                body.operation,
                body.arguments,
                revalidate=lambda: authenticate(request),
            )

    return router
