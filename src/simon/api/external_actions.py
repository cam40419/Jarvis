"""Authenticated review endpoints; no provider writes occur while creating proposals."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from simon.domain.external_actions import (
    ExternalActionDraft,
    ExternalActionProposal,
    ExternalQuoteRequest,
    ProposeExternalAction,
    ReconcileExternalAction,
    ReviewExternalAction,
)
from simon.domain.models import ActorContext
from simon.services.external_actions import ExternalActionService


def external_actions_router(
    service: ExternalActionService,
    authenticate: Callable[[Request], ActorContext],
) -> APIRouter:
    router = APIRouter(prefix="/v1/external-actions", tags=["external action review"])

    @router.get("/providers")
    def providers(
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> tuple[dict[str, Any], ...]:
        return service.provider_statuses(actor)

    @router.post("/quotes")
    def quote(
        body: ExternalQuoteRequest,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ExternalActionDraft:
        return service.quote(actor, body)

    @router.get("")
    def listing(
        actor: Annotated[ActorContext, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> tuple[ExternalActionProposal, ...]:
        return service.list_actions(actor, offset=offset, limit=limit)

    @router.post("", status_code=201)
    def propose(
        body: ProposeExternalAction,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ExternalActionProposal:
        return service.propose(actor, body.draft, body.idempotency_key)

    @router.get("/{identifier}")
    def get(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ExternalActionProposal:
        return service.get(actor, identifier)

    @router.post("/{identifier}/confirm")
    def confirm(
        identifier: UUID,
        body: ReviewExternalAction,
        request: Request,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ExternalActionProposal:
        return service.decide(
            actor,
            identifier,
            review_digest=body.review_digest,
            confirm=True,
            revalidate=lambda: authenticate(request),
        )

    @router.post("/{identifier}/cancel")
    def cancel(
        identifier: UUID,
        body: ReviewExternalAction,
        request: Request,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ExternalActionProposal:
        return service.decide(
            actor,
            identifier,
            review_digest=body.review_digest,
            confirm=False,
            revalidate=lambda: authenticate(request),
        )

    @router.post("/{identifier}/refresh")
    def refresh(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ExternalActionProposal:
        return service.refresh(actor, identifier)

    @router.post("/{identifier}/mark-interrupted")
    def interrupted(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ExternalActionProposal:
        return service.mark_interrupted_unknown(actor, identifier)

    @router.post("/{identifier}/reconcile")
    def reconcile(
        identifier: UUID,
        body: ReconcileExternalAction,
        request: Request,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ExternalActionProposal:
        return service.reconcile(actor, identifier, body, revalidate=lambda: authenticate(request))

    return router
