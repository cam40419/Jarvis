"""Shared API/worker binding for optional commitment providers."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from simon.adapters.external_action_providers import (
    ExternalActionProviders,
    load_external_providers,
)
from simon.adapters.external_action_tools import ExternalActionToolTransport
from simon.adapters.optional_http import BoundedHTTP
from simon.adapters.tool_transports import ToolHandler
from simon.config import Settings
from simon.domain.execution import EnvironmentLease
from simon.domain.models import ActorContext
from simon.domain.ports import Store
from simon.services.agent_dispatcher import TransportFactory
from simon.services.agent_platform import platform_credentials
from simon.services.audit import AuditService
from simon.services.external_actions import ExternalActionService


def external_action_service(settings: Settings, store: Store) -> ExternalActionService:
    return ExternalActionService(
        store,
        AuditService(store),
        load_external_providers(settings.external_providers_file),
        ExternalActionProviders(BoundedHTTP(environ=platform_credentials())),
    )


def external_tool_status(
    service: ExternalActionService,
    actor: ActorContext,
    tool_id: str,
) -> dict[str, Any]:
    if tool_id == "external_actions.quote" and not any(
        item["available"] and item["kind"] == "gateway" for item in service.provider_statuses(actor)
    ):
        return {"available": False, "reason": "Configure a merchant quote and booking provider"}
    return {"available": True, "reason": None}


def external_transport_factory(
    base: TransportFactory,
    service: ExternalActionService,
) -> TransportFactory:
    def factory(
        actor: ActorContext,
        run_id: UUID,
        revalidate: Callable[[], ActorContext],
        lease: EnvironmentLease | None,
    ) -> dict[str, ToolHandler]:
        return {
            **base(actor, run_id, revalidate, lease),
            "external_actions": ExternalActionToolTransport(
                service,
                actor_id=actor.actor_id,
                household_id=actor.household_id,
                run_id=run_id,
                revalidate=revalidate,
            ),
        }

    return factory
