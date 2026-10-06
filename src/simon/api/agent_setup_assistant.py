"""Authenticated, non-persistent recommendations for editable agent and team drafts."""

from collections.abc import Callable
from threading import Lock
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from simon.domain.agent_setup_assistant import (
    AgentSetupAssistantRequest,
    AgentSetupAssistantResponse,
)
from simon.domain.errors import ModelBusyError
from simon.domain.models import ActorContext
from simon.services.agent_setup_assistant import AgentSetupAssistantService


def agent_setup_assistant_router(
    service: AgentSetupAssistantService,
    authenticate: Callable[[Request], ActorContext],
) -> APIRouter:
    router = APIRouter(prefix="/v1/agent-platform", tags=["agent setup"])
    active: set[tuple[UUID, UUID]] = set()
    lock = Lock()

    @router.post("/setup-assistant")
    def recommend(
        body: AgentSetupAssistantRequest,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> AgentSetupAssistantResponse:
        key = (actor.workspace_id, actor.actor_id)
        with lock:
            if key in active:
                raise ModelBusyError("Your setup recommendation is still being prepared.")
            active.add(key)
        try:
            return service.recommend(actor, body)
        finally:
            with lock:
                active.discard(key)

    return router
