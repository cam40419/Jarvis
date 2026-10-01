import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field

from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.local_files import LocalList, LocalPath, LocalRead
from simon.domain.models import ActorContext, StrictModel
from simon.services.canonical import digest
from simon.services.local_files import MAX_FILE, LocalFileService, revision


class LocalAction(StrictModel):
    name: str = Field(pattern=r"^local_[a-z_]+$", max_length=50)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)


def preview_media_type(raw: bytes) -> str:
    """Recognize only browser-safe raster formats and PDF, regardless of filename."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw.startswith(b"%PDF-"):
        return "application/pdf"
    raise ValidationError(
        "Preview supports PNG, JPEG, GIF, WebP and PDF files. Use Download or Read.",
    )


def local_file_router(
    service: LocalFileService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v1/local-files", tags=["local files"])

    def read_authorized(
        actor: ActorContext, request: Request, root: str, path: str,
    ) -> tuple[Path, bytes]:
        values = LocalPath(root=root, path=path)
        target = service.path(actor, values.root, values.path)
        raw = service.blob(target)
        current = authenticate(request)
        service.authorize(current)
        if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
            raise AuthorizationError("Local access changed.")
        # Recheck project visibility and root configuration after reading the bytes.
        if service.path(current, values.root, values.path) != target:
            raise AuthorizationError("Local file location changed.")
        return target, raw

    @router.get("/roots")
    def roots(actor: Annotated[ActorContext, Depends(authenticate)]) -> dict[str, Any]:
        return service.run(actor, "local_files_roots", {}, "read", lambda: actor)

    @router.get("/list")
    def listing(
        actor: Annotated[ActorContext, Depends(authenticate)],
        request: Request,
        root: Annotated[str, Query(min_length=1, max_length=100)],
        path: Annotated[str, Query(max_length=1000)] = "",
        offset: Annotated[int, Query(ge=0, le=20000)] = 0,
    ) -> dict[str, Any]:
        values = LocalList(root=root, path=path, offset=offset).model_dump()
        return service.run(actor, "local_files_list", values, "read", lambda: authenticate(request))

    @router.get("/read")
    def read(
        actor: Annotated[ActorContext, Depends(authenticate)],
        request: Request,
        root: Annotated[str, Query(min_length=1, max_length=100)],
        path: Annotated[str, Query(max_length=1000)],
        offset: Annotated[int, Query(ge=0, le=2000000)] = 0,
    ) -> dict[str, Any]:
        values = LocalRead(root=root, path=path, offset=offset).model_dump()
        return service.run(actor, "local_file_read", values, "read", lambda: authenticate(request))

    @router.get("/download")
    def download(
        actor: Annotated[ActorContext, Depends(authenticate)],
        request: Request,
        root: Annotated[str, Query(min_length=1, max_length=100)],
        path: Annotated[str, Query(max_length=1000)],
    ) -> Response:
        target, raw = read_authorized(actor, request, root, path)
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(target.name, safe=""),
                "Cache-Control": "no-store",
            },
        )

    @router.get("/preview")
    def preview(
        actor: Annotated[ActorContext, Depends(authenticate)],
        request: Request,
        root: Annotated[str, Query(min_length=1, max_length=100)],
        path: Annotated[str, Query(max_length=1000)],
    ) -> Response:
        target, raw = read_authorized(actor, request, root, path)
        return Response(raw, media_type=preview_media_type(raw), headers={
            "Content-Disposition": "inline; filename*=UTF-8''" + quote(target.name, safe=""),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
        })

    @router.post("/action")
    def action(
        body: LocalAction, request: Request, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return service.run(
            actor,
            body.name,
            body.arguments,
            "ui:" + body.idempotency_key,
            lambda: authenticate(request),
        )

    @router.post("/upload")
    async def upload(
        request: Request,
        actor: Annotated[ActorContext, Depends(authenticate)],
        root: Annotated[str, Query(min_length=1, max_length=100)],
        path: Annotated[str, Query(max_length=1000)],
        idempotency_key: Annotated[str, Query(min_length=8, max_length=200)],
    ) -> dict[str, Any]:
        values = LocalPath(root=root, path=path)
        service.authorize(actor, write=True)
        service.path(actor, values.root, values.path)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_FILE:
                raise ValidationError("Uploads are limited to 50 MB.")
            raw.extend(chunk)

        def save() -> dict[str, Any]:
            current = authenticate(request)
            service.authorize(current, write=True)
            if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Local access changed.")
            with service.store.transaction(actor.household_id):
                result, _ = service.store.execute_once(
                    f"local-upload:{actor.household_id}:{actor.actor_id}",
                    idempotency_key,
                    digest({**values.model_dump(), "revision": revision(bytes(raw))}),
                    lambda: service.publish(actor, root, path, bytes(raw)),
                )
                return result

        return await asyncio.to_thread(save)

    return router
