"""Worker tools prepare durable reviews; confirmation is deliberately absent."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from pydantic import Field, ValidationError

from simon.domain.errors import AuthorizationError
from simon.domain.external_actions import ExternalQuoteRequest, ProposeExternalAction
from simon.domain.models import ActorContext, Channel, StrictModel
from simon.domain.tool_catalog import ToolCatalogError, ToolDefinition, ToolExecutionContext
from simon.services.external_actions import ExternalActionService


class ActionStatusRequest(StrictModel):
    action_id: UUID


class ProviderListRequest(StrictModel):
    limit: int = Field(default=50, ge=1, le=50)


SCHEMAS: dict[str, type[StrictModel]] = {
    "external_actions.propose": ProposeExternalAction,
    "external_actions.status": ActionStatusRequest,
    "external_actions.providers": ProviderListRequest,
    "external_actions.quote": ExternalQuoteRequest,
}


def external_action_tool_definitions(
    *,
    enabled: bool = False,
    include_quote: bool = False,
) -> tuple[ToolDefinition, ...]:
    descriptions = {
        "external_actions.propose": (
            "Save exact purchase, booking, reservation or prerecorded-call details for review. "
            "This does not place an order, reserve, pay or call. Return the review action ID."
        ),
        "external_actions.status": "Read the durable state and receipt of an owned action.",
        "external_actions.providers": (
            "List this account's configured external providers and honest configuration blockers."
        ),
        "external_actions.quote": (
            "Request a nonbinding quote/availability from a configured merchant gateway. "
            "This does not hold availability or create a purchase, reservation or booking."
        ),
    }
    return tuple(
        ToolDefinition(
            id=key,
            description=value,
            transport="external_actions",
            enabled=enabled,
            configured=True,
            categories=frozenset({"external_actions"}),
            capabilities=frozenset({key}),
            required_scopes=frozenset({"jobs:write" if key.endswith("propose") else "jobs:read"}),
            side_effect=key.endswith("propose"),
            action_policy="write" if key.endswith("propose") else "read",
            input_schema=SCHEMAS[key].model_json_schema(),
            output_schema={"type": "object"},
            settings={"network": key.endswith("quote")},
        )
        for key, value in descriptions.items()
        if include_quote or not key.endswith("quote")
    )


def external_action_configuration_reason(definition: ToolDefinition) -> str | None:
    if definition.transport != "external_actions" or definition.id not in SCHEMAS:
        return "Unknown external-action review operation"
    write = definition.id == "external_actions.propose"
    if definition.input_schema != SCHEMAS[definition.id].model_json_schema():
        return "External-action tools require the canonical argument schema"
    if (
        definition.side_effect != write
        or definition.action_policy != ("write" if write else "read")
        or not {"jobs:write" if write else "jobs:read"} <= definition.required_scopes
    ):
        return "External-action review tools require canonical actions and scopes"
    return None


class ExternalActionToolTransport:
    def __init__(
        self,
        service: ExternalActionService,
        *,
        actor_id: UUID,
        workspace_id: UUID,
        run_id: UUID,
        revalidate: Callable[[], ActorContext] | None = None,
    ) -> None:
        self.service, self.actor_id, self.workspace_id = service, actor_id, workspace_id
        self.run_id, self.revalidate = run_id, revalidate

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        write = definition.id == "external_actions.propose"
        scope = "jobs:write" if write else "jobs:read"
        if (
            (context.actor_id, context.workspace_id, context.run_id)
            != (self.actor_id, self.workspace_id, self.run_id)
            or definition.id not in context.allowed_tool_ids
            or scope not in context.scopes
            or not definition.required_scopes <= context.scopes
            or (write and context.authorized_action not in {"write", "external_commitment"})
        ):
            raise AuthorizationError("External-action review tool was not authorized")
        reason = external_action_configuration_reason(definition)
        if reason or not definition.enabled or not definition.configured:
            raise ToolCatalogError(reason or "External-action review tool is unavailable")
        try:
            checked = SCHEMAS[definition.id].model_validate(arguments)
        except ValidationError:
            raise ToolCatalogError("External-action arguments do not match the operation") from None
        scopes = context.scopes
        if self.revalidate is not None:
            current = self.revalidate()
            if (current.actor_id, current.workspace_id) != (self.actor_id, self.workspace_id):
                raise AuthorizationError("External-action access changed")
            scopes &= current.scopes
            if scope not in scopes or not definition.required_scopes <= scopes:
                raise AuthorizationError("External-action permission was revoked")
        actor = ActorContext(
            actor_id=context.actor_id,
            workspace_id=context.workspace_id,
            channel=Channel.WORKER,
            scopes=scopes,
        )
        if isinstance(checked, ProposeExternalAction):
            return self.service.propose(
                actor, checked.draft, checked.idempotency_key, run_id=context.run_id
            ).model_dump(mode="json")
        if isinstance(checked, ActionStatusRequest):
            return self.service.get(actor, checked.action_id).model_dump(mode="json")
        if isinstance(checked, ExternalQuoteRequest):
            return self.service.quote(actor, checked).model_dump(mode="json")
        assert isinstance(checked, ProviderListRequest)
        return {"providers": list(self.service.provider_statuses(actor)[: checked.limit])}
