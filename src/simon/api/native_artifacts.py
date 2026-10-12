"""Authenticated artifact files and human-authorized review/acceptance commands."""

from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.native_artifact_commands import (
    DecideNativeArtifact,
    ReviewNativeArtifact,
    UpdateArtifactReviewPolicy,
    UploadNativeArtifact,
)
from simon.domain.native_artifacts import NativeReviewPolicy
from simon.services.native_artifacts import NativeArtifactService


def native_artifacts_router(
    service: NativeArtifactService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v2/projects/{project_id}/artifacts", tags=["artifacts and review"])

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

    @router.put("/policy")
    def policy(
        project_id: UUID,
        body: UpdateArtifactReviewPolicy,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> NativeReviewPolicy:
        return service.update_policy(current, project_id, body)

    @router.post("/upload", status_code=201)
    def upload(
        project_id: UUID,
        body: UploadNativeArtifact,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.upload(current, project_id, body)

    @router.get("/operations/{key}")
    def receipt(
        project_id: UUID, key: str, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.operation_receipt(current, project_id, key)

    @router.get("/{artifact_id}")
    def detail(
        project_id: UUID, artifact_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.detail(current, project_id, artifact_id)

    @router.get("/{artifact_id}/revisions/{revision_id}/preview")
    def preview(
        project_id: UUID,
        artifact_id: UUID,
        revision_id: UUID,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.preview(current, project_id, artifact_id, revision_id)

    @router.get("/{artifact_id}/revisions/{revision_id}/content")
    def content(
        project_id: UUID,
        artifact_id: UUID,
        revision_id: UUID,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> Response:
        revision, raw = service.read_content(current, project_id, artifact_id, revision_id)
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(revision.filename, safe=""),
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "ETag": '"' + revision.sha256 + '"',
            },
        )

    @router.post("/{artifact_id}/reviews")
    def review(
        project_id: UUID,
        artifact_id: UUID,
        body: ReviewNativeArtifact,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.review(current, project_id, artifact_id, body)

    @router.post("/{artifact_id}/decisions")
    def decision(
        project_id: UUID,
        artifact_id: UUID,
        body: DecideNativeArtifact,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.decide(current, project_id, artifact_id, body)

    return router
