import asyncio
from collections.abc import Callable
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


def local_file_router(
    service: LocalFileService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v1/local-files", tags=["local files"])

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
        values = LocalPath(root=root, path=path)
        target = service.path(actor, values.root, values.path)
        raw = service.blob(target)
        current = authenticate(request)
        service.authorize(current)
        if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
            raise AuthorizationError("Local access changed.")
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(target.name, safe=""),
                "Cache-Control": "no-store",
            },
        )

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
