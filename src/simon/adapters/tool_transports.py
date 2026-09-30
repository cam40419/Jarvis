"""Bound tool execution. Network writes are never automatically retried."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from referencing import Registry
from referencing.exceptions import Unresolvable

from simon.domain.errors import AuthorizationError
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
    ToolExecutionResult,
)


class ToolHandler(Protocol):
    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]: ...


class TransportRegistry:
    """CLI, desktop and container handlers must be bound to an execution backend explicitly."""

    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    @property
    def transports(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def register(self, transport: str, handler: ToolHandler) -> None:
        if transport in self._handlers:
            raise ToolCatalogError(f"Execution transport already registered: {transport}")
        self._handlers[transport] = handler

    def execute(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        # Revalidate mutable nested schema/config dictionaries at the dispatch boundary.
        definition = ToolDefinition.model_validate(definition.model_dump(mode="python"))
        if definition.id not in context.allowed_tool_ids:
            raise AuthorizationError("Tool was not granted to this assignment")
        if not definition.required_scopes <= context.scopes:
            raise AuthorizationError("Tool permission is missing")
        levels = {"read": 0, "write": 1, "external_commitment": 2}
        if levels[definition.action_policy] > levels[context.authorized_action]:
            raise AuthorizationError("The action policy has not authorized this tool operation")
        if not definition.enabled or not definition.configured:
            raise ToolCatalogError("Tool is disabled or unconfigured")
        if not definition.environment_capabilities <= context.environment_capabilities:
            raise ToolCatalogError("Execution environment cannot support this tool")
        handler = self._handlers.get(definition.transport)
        if handler is None:
            raise ToolCatalogError("No execution handler is bound to this tool transport")
        try:
            Draft202012Validator(definition.input_schema, registry=Registry()).validate(arguments)
        except (SchemaValidationError, Unresolvable):
            raise ToolCatalogError(
                "Tool arguments do not match the declared input schema"
            ) from None
        output = handler(definition, arguments, context)
        try:
            if not isinstance(output, dict):
                raise ToolExecutionError(
                    "Tool returned a non-object response", unknown=definition.side_effect
                )
            Draft202012Validator(definition.output_schema, registry=Registry()).validate(output)
        except (SchemaValidationError, Unresolvable):
            raise ToolExecutionError(
                "Tool response does not match the declared output schema",
                unknown=definition.side_effect,
            ) from None
        return ToolExecutionResult(
            tool_id=definition.id,
            actor_id=context.actor_id,
            household_id=context.household_id,
            run_id=context.run_id,
            agent_id=context.agent_id,
            invocation_id=context.invocation_id,
            output=output,
        )


class HttpJsonTransport:
    """POST a fixed JSON envelope to a configured endpoint, with bounded response size."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 30,
        max_response_bytes: int = 2_000_000,
        transport: httpx.BaseTransport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 300 or not 0 < max_response_bytes <= 10_000_000:
            raise ValueError("Tool transport limits are outside their supported range")
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self._transport = transport
        self._environ = os.environ if environ is None else environ

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        if not definition.endpoint:
            raise ToolCatalogError("HTTP tool has no configured endpoint")
        headers = {
            "X-Actor-ID": str(context.actor_id),
            "X-Household-ID": str(context.household_id),
            "X-Run-ID": str(context.run_id),
            "X-Agent-ID": context.agent_id,
            "X-Invocation-ID": str(context.invocation_id),
        }
        if definition.credential_env:
            secret = self._environ.get(definition.credential_env, "").strip()
            if not secret:
                raise ToolCatalogError("Tool credential environment variable is unavailable")
            headers["Authorization"] = f"Bearer {secret}"
        try:
            with httpx.Client(  # noqa: SIM117
                timeout=self.timeout_seconds,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                with client.stream(
                    "POST",
                    definition.endpoint,
                    headers=headers,
                    json={
                        "tool": definition.id,
                        "arguments": arguments,
                        "context": {
                            "actor_id": str(context.actor_id),
                            "household_id": str(context.household_id),
                            "run_id": str(context.run_id),
                            "agent_id": context.agent_id,
                            "invocation_id": str(context.invocation_id),
                        },
                    },
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise ToolExecutionError(
                            "Tool endpoint rejected the request",
                            unknown=definition.side_effect
                            and response.status_code not in {400, 401, 403, 404, 422},
                        )
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_response_bytes:
                            raise ToolExecutionError(
                                "Tool response exceeded its configured size limit",
                                unknown=definition.side_effect,
                            )
                    result: Any = json.loads(body)
                    if not isinstance(result, dict):
                        raise ValueError("Non-object response")
                    return result
        except (httpx.HTTPError, ValueError):
            raise ToolExecutionError(
                "Tool connection failed or its response was invalid",
                unknown=definition.side_effect,
            ) from None


class MCPClient(Protocol):
    """Adapter for an authenticated MCP session owned by an execution environment."""

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: ToolExecutionContext,
    ) -> dict[str, Any]: ...


class MCPTransport:
    def __init__(self, client: MCPClient) -> None:
        self.client = client

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        name = definition.settings.get("tool_name", definition.id)
        if not isinstance(name, str) or not name:
            raise ToolCatalogError("MCP tool name is invalid")
        try:
            return self.client.call_tool(name, arguments, context=context)
        except (AuthorizationError, ToolCatalogError, ToolExecutionError):
            raise
        except Exception:
            raise ToolExecutionError(
                "MCP tool execution failed", unknown=definition.side_effect
            ) from None
