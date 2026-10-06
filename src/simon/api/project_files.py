import base64
import binascii
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from simon.domain.errors import ValidationError
from simon.domain.models import ActorContext, StrictModel
from simon.domain.project_files import (
    DriveBrowse,
    ProjectBind,
    ProjectCreate,
    ProjectFile,
    ProjectFileCreate,
    ProjectFiles,
    ProjectTrash,
    ProjectUnlink,
)
from simon.services.project_files import ProjectFileService


class TrashProjectFile(ProjectTrash):
    idempotency_key: str = Field(min_length=8, max_length=200)


class UploadProjectFile(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(max_length=13981016)
    folder_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    idempotency_key: str = Field(min_length=8, max_length=200)


def project_router(
    service: ProjectFileService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v1/projects", tags=["project files"])

    @router.get("")
    def projects(actor: Annotated[ActorContext, Depends(authenticate)]) -> list[dict[str, Any]]:
        return service.list(actor)

    @router.post("")
    def create(
        body: ProjectCreate, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.create(actor, body)

    @router.post("/link-drive")
    def link(
        body: ProjectBind, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.bind(actor, body)

    @router.post("/drive/browse")
    def browse(
        body: DriveBrowse, request: Request, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.browse(actor, body, lambda: authenticate(request))

    @router.post("/unlink-drive")
    def unlink(
        body: ProjectUnlink, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.unlink(actor, body)

    @router.post("/trash-drive-item")
    def trash(
        body: TrashProjectFile,
        request: Request,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.trash(
            actor,
            ProjectTrash.model_validate(body.model_dump(exclude={"idempotency_key"})),
            "trash-ui:" + body.idempotency_key,
            lambda: authenticate(request),
        )

    @router.post("/{identifier}/sync")
    def sync(
        identifier: UUID, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.sync(actor, identifier, force=True)

    @router.get("/{identifier}/files")
    def files(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        folder_id: str | None = None,
        query: str = "",
        page_token: str = "",
    ) -> dict[str, Any]:
        return service.files(
            actor,
            ProjectFiles(
                project_id=identifier, folder_id=folder_id, query=query, page_token=page_token
            ),
        )

    @router.get("/{identifier}/files/{file_id}")
    def read(
        identifier: UUID, file_id: str, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.read(actor, ProjectFile(project_id=identifier, file_id=file_id))

    @router.post("/{identifier}/upload")
    def upload(
        identifier: UUID,
        body: UploadProjectFile,
        request: Request,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ValidationError("Invalid file upload.") from None
        if len(content) > 10 * 1024 * 1024:
            raise ValidationError(
                "Upload files up to 10 MB here; larger files can be added in Drive."
            )
        return service.create_file(
            actor,
            ProjectFileCreate(project_id=identifier, name=body.name, folder_id=body.folder_id),
            "upload:" + body.idempotency_key,
            lambda: authenticate(request),
            raw=content,
            media_type=service.api.media(body.name, "text"),
            retry_create=True,
        )

    @router.get("/{identifier}")
    def project_page(
        identifier: UUID, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        project = service.project(actor, identifier)
        tasks = service.connected.tasks
        from simon.services.work_sessions import WorkSessionService

        return {
            **project.model_dump(mode="json"),
            "drive": service.view(actor, service.binding(actor, identifier)),
            "tasks": [
                task.model_dump(mode="json")
                for task in tasks.list(actor, 0, 500)
                if task.project_id == identifier
            ]
            if tasks
            else [],
            "artifacts": [
                item.model_dump(mode="json") for item in tasks.artifacts(actor, identifier)
            ]
            if tasks
            else [],
            "sessions": WorkSessionService(tasks).list(actor, project_id=identifier)
            if tasks
            else [],
            "activity": [
                service.receipt(item)
                for item in service.store.project_file_operations(
                    actor.workspace_id, actor.actor_id, identifier, 30
                )
            ],
        }

    @router.get("/{identifier}/history")
    def history(
        identifier: UUID, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> list[dict[str, Any]]:
        service.project(actor, identifier)
        return [
            service.receipt(item)
            for item in service.store.project_file_operations(
                actor.workspace_id, actor.actor_id, identifier, 100
            )
        ]

    return router
