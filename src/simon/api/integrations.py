"""Authenticated, CSRF-protected connection management without secret readback."""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response

from simon.domain.integrations import ConnectIntegration
from simon.domain.models import ActorContext
from simon.services.integrations import IntegrationService


def integrations_router(
    service: IntegrationService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v1/connections/integrations", tags=["connections"])

    @router.get("")
    def connections(actor: Annotated[ActorContext, Depends(authenticate)]) -> list[dict[str, Any]]:
        return service.list(actor)

    @router.get("/setup")
    def setup(actor: Annotated[ActorContext, Depends(authenticate)]) -> dict[str, Any]:
        service.authorize(actor)
        return {
            "can_configure_google_app": actor.actor_id == service.settings.account_admin_actor_id,
            "can_configure_email": service.can_configure_email(actor),
            "google_redirect_uri": service.settings.public_origin
            + service.settings.public_path
            + "/auth/google/callback",
        }

    @router.post("/{provider}")
    def connect(
        provider: str,
        body: ConnectIntegration,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.connect(actor, provider, body)

    @router.delete("/{identifier}", status_code=204)
    def disconnect(
        identifier: str, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> Response:
        service.disconnect(actor, identifier)
        return Response(status_code=204)

    @router.post("/{identifier}/test")
    def test_connection(
        identifier: str,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.test(actor, identifier)

    @router.post("/{provider}/{identifier}")
    def reconnect(
        provider: str,
        identifier: str,
        body: ConnectIntegration,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return service.connect(actor, provider, body, identifier)

    return router
