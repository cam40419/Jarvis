"""Human-authorized project evidence intake and reviewed automatic staffing."""

from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.native_intake import (
    AnalyzeIntake,
    IntakeRun,
    NativeIntake,
    SourceUpload,
    UpdateNativeIntake,
)
from simon.domain.native_projects import VersionedNativeCommand
from simon.services.identity import IDENTITY_LOCK
from simon.services.native_intake import NativeIntakeService, source_metadata


def native_intake_router(
    service: NativeIntakeService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v2/projects/{project_id}/intake", tags=["project intake"])

    def actor(
        request: Request, x_workspace_id: Annotated[UUID | None, Header()] = None
    ) -> ActorContext:
        current = authenticate(request)
        if x_workspace_id is not None and current.workspace_id != x_workspace_id:
            raise AuthorizationError("Workspace changed. Reload before continuing.")
        return current

    @router.get("")
    def view(project_id: UUID, current: Annotated[ActorContext, Depends(actor)]) -> dict[str, Any]:
        return service.view(current, project_id)

    @router.put("")
    def update(
        project_id: UUID, body: UpdateNativeIntake, current: Annotated[ActorContext, Depends(actor)]
    ) -> NativeIntake:
        return service.update(current, project_id, body)

    @router.post("/sources", status_code=201)
    def upload(
        project_id: UUID, body: SourceUpload, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return source_metadata(service.upload(current, project_id, body))

    @router.get("/sources/{source_id}/content")
    def content(
        project_id: UUID, source_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> Response:
        source, raw = service.read_source(current, project_id, source_id)
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(source.filename, safe=""),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/sources/{source_id}/text")
    def text(
        project_id: UUID, source_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        with (
            service.store.transaction(IDENTITY_LOCK),
            service.store.transaction(current.workspace_id),
        ):
            source = service.source(current, project_id, source_id)
            return {
                "text": source.text,
                "truncated": source.truncated,
                "redactions": source.redactions,
                "extraction_status": source.extraction_status,
            }

    @router.post("/sources/{source_id}/revoke")
    def revoke(
        project_id: UUID,
        source_id: UUID,
        body: VersionedNativeCommand,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> NativeIntake:
        return service.revoke(current, project_id, source_id, body)

    @router.post("/analyze")
    def analyze(
        project_id: UUID, body: AnalyzeIntake, current: Annotated[ActorContext, Depends(actor)]
    ) -> IntakeRun:
        return service.analyze(current, project_id, body)

    @router.get("/runs/{run_id}")
    def run(
        project_id: UUID, run_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> IntakeRun:
        return service.get_run(current, project_id, run_id)

    @router.post("/runs/{run_id}/apply")
    def apply(
        project_id: UUID,
        run_id: UUID,
        body: VersionedNativeCommand,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> IntakeRun:
        return service.apply(current, project_id, run_id, body)

    @router.post("/runs/{run_id}/cancel")
    def cancel(
        project_id: UUID,
        run_id: UUID,
        body: VersionedNativeCommand,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> IntakeRun:
        return service.cancel(current, project_id, run_id, body)

    return router
