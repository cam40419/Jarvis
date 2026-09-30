from collections.abc import Callable
from contextlib import suppress
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from simon.domain.displays import ConfigureDisplay, CreateDisplay, DisplayDevice
from simon.domain.models import ActorContext, Channel, utc_now
from simon.services.displays import DisplayService

DISPLAY_COOKIE = "simon_display_token"


def display_router(
    displays: DisplayService,
    actor_dependency: Callable[..., ActorContext],
    services: Any,
    page: Callable[[str], HTMLResponse],
) -> APIRouter:
    router = APIRouter(tags=["displays"])
    Actor = Annotated[ActorContext, Depends(actor_dependency)]

    def public(device: DisplayDevice) -> dict[str, Any]:
        return {
            "id": str(device.id),
            "name": device.name,
            "revision": device.revision,
            "configuration": device.configuration.model_dump(mode="json"),
            "images": [image.model_dump(mode="json") for image in device.images],
            "created_at": device.created_at.isoformat(),
            "updated_at": device.updated_at.isoformat(),
            "last_seen_at": device.last_seen_at.isoformat() if device.last_seen_at else None,
            "online": bool(
                device.last_seen_at
                and (utc_now() - device.last_seen_at).total_seconds() < 90
            ),
        }

    @router.get("/displays", include_in_schema=False)
    def displays_page() -> HTMLResponse:
        return page("displays.html")

    @router.get("/v1/displays")
    def list_displays(actor: Actor) -> list[dict[str, Any]]:
        return [public(device) for device in displays.list(actor)]

    @router.post("/v1/displays", status_code=201)
    def create_display(body: CreateDisplay, actor: Actor) -> dict[str, Any]:
        device, token = displays.create(actor, body)
        base = services.settings.public_origin + services.settings.public_path
        return {
            **public(device),
            "provisioning_url": f"{base}/display/{device.id}?token={quote(token)}",
        }

    @router.patch("/v1/displays/{display_id}")
    def configure_display(
        display_id: UUID, body: ConfigureDisplay, actor: Actor
    ) -> dict[str, Any]:
        return public(displays.configure(actor, display_id, body))

    @router.post("/v1/displays/{display_id}/images", status_code=201)
    def upload_image(
        display_id: UUID,
        actor: Actor,
        image: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        saved = displays.add_image(
            actor,
            display_id,
            image.filename or "image",
            image.content_type or "application/octet-stream",
            image.file,
        )
        return saved.model_dump(mode="json")

    @router.delete("/v1/displays/{display_id}/images/{image_id}", status_code=204)
    def delete_image(display_id: UUID, image_id: UUID, actor: Actor) -> None:
        displays.delete_image(actor, display_id, image_id)

    @router.get("/v1/displays/{display_id}/managed-images/{image_id}")
    def managed_display_image(display_id: UUID, image_id: UUID, actor: Actor) -> FileResponse:
        device = displays.get(actor, display_id)
        path, image = displays.image_path(device, image_id)
        return FileResponse(
            path,
            media_type=image.media_type,
            filename=image.filename,
            content_disposition_type="inline",
        )

    @router.get("/display/{display_id}", include_in_schema=False, response_model=None)
    def display_page(
        display_id: UUID,
        token: str = "",
        simon_display_token: Annotated[str, Cookie()] = "",
    ) -> HTMLResponse | RedirectResponse:
        supplied = token or simon_display_token
        displays.authenticate(display_id, supplied)
        if token:
            clean = services.settings.public_path + f"/display/{display_id}"
            response = RedirectResponse(clean, status_code=303)
            response.set_cookie(
                DISPLAY_COOKIE,
                token,
                httponly=True,
                secure=services.settings.secure_cookies,
                samesite="strict",
                max_age=31_536_000,
                path="/",
            )
            return response
        return page("display.html")

    def device_auth(display_id: UUID, token: str) -> DisplayDevice:
        return displays.authenticate(display_id, token, touch=True)

    @router.get("/v1/displays/{display_id}/state")
    def display_state(
        display_id: UUID,
        simon_display_token: Annotated[str, Cookie()] = "",
    ) -> dict[str, Any]:
        device = device_auth(display_id, simon_display_token)
        actor = ActorContext(
            actor_id=device.created_by,
            household_id=device.household_id,
            channel=Channel.API,
            scopes=frozenset({"home:read", "jobs:read", "threads:read"}),
        )
        data: dict[str, Any] = {"power": None, "jobs": [], "printer": None}
        with suppress(Exception):
            data["power"] = services.power.overview(actor)
        try:
            tasks = services.tasks.list(actor)
            data["jobs"] = [
                {
                    "id": str(task.id),
                    "title": task.input.get("title") or task.input.get("objective") or task.kind,
                    "status": task.status.value,
                    "updated_at": task.updated_at.isoformat(),
                }
                for task in tasks
                if task.status.value in {"queued", "running", "waiting", "needs_human"}
            ]
        except Exception:
            pass
        with suppress(Exception):
            data["jobs"].extend(
                {
                    "id": str(run.id),
                    "title": run.name,
                    "status": run.status,
                    "updated_at": run.created_at.isoformat(),
                }
                for run in services.workflows.runs(actor, 0, 50)
                if run.status in {"queued", "running", "waiting", "paused", "needs_attention"}
            )
        with suppress(Exception):
            data["jobs"].extend(
                {
                    "id": batch["id"],
                    "title": "Print batch " + batch["id"][:8],
                    "status": batch["state"],
                    "updated_at": batch["created"],
                }
                for batch in services.print_batches.batches(actor)
                if batch["state"] in {"staged", "running", "waiting_for_plate", "needs_attention"}
            )
        data["jobs"] = data["jobs"][:8]
        with suppress(Exception):
            data["printer"] = services.printer_status.status(actor)
        return {
            "display": public(device),
            "data": data,
            "server_time": utc_now().isoformat(),
        }

    @router.get("/v1/displays/{display_id}/images/{image_id}")
    def display_image(
        display_id: UUID,
        image_id: UUID,
        simon_display_token: Annotated[str, Cookie()] = "",
    ) -> FileResponse:
        device = displays.authenticate(display_id, simon_display_token)
        path, image = displays.image_path(device, image_id)
        return FileResponse(
            path,
            media_type=image.media_type,
            filename=image.filename,
            content_disposition_type="inline",
        )

    return router
