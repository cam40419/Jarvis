"""Execute an administrator-configured argv prefix inside one owned worker lease."""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from simon.domain.errors import AuthorizationError
from simon.domain.execution import EnvironmentLease, ExecutionCommand, ExecutionResult
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)


class LeasedCommandExecutor(Protocol):
    def execute(
        self, lease_id: UUID, command: ExecutionCommand, *, attempt_id: UUID, fencing_token: int
    ) -> ExecutionResult: ...


class EnvironmentCommandTransport:
    """No host or shell fallback. Fixed argv prefixes are trusted administrator grants.

    Argument constraints belong in each tool's input JSON Schema. A fixed executable
    alone does not restrict what that application can do inside the leased resource.
    The manager enforces current lease status, ownership, and fencing at execution.
    """

    def __init__(
        self,
        manager: LeasedCommandExecutor,
        lease: EnvironmentLease,
        *,
        actor_id: UUID,
        run_id: UUID,
        max_timeout_seconds: int = 60,
        max_output_bytes: int = 65536,
    ) -> None:
        if (
            isinstance(max_timeout_seconds, bool)
            or not 1 <= max_timeout_seconds <= 600
            or isinstance(max_output_bytes, bool)
            or not 1024 <= max_output_bytes <= 1048576
        ):
            raise ValueError("Environment transport limits are outside their supported range")
        if lease.status != "active":
            raise ToolCatalogError("An environment tool requires an active lease")
        self.manager = manager
        self.lease = lease.model_copy(deep=True)
        self.actor_id = actor_id
        self.run_id = run_id
        self.max_timeout_seconds = max_timeout_seconds
        self.max_output_bytes = max_output_bytes

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        assignment = self.lease.plan.request
        if (
            context.actor_id != self.actor_id
            or context.workspace_id != assignment.workspace_id
            or context.run_id != self.run_id
            or context.agent_id != assignment.agent_id
        ):
            raise AuthorizationError("Tool execution does not belong to this worker lease")
        if (
            definition.id not in context.allowed_tool_ids
            or not definition.required_scopes <= context.scopes
            or context.authorized_action not in {"write", "external_commitment"}
        ):
            raise AuthorizationError("Environment command execution was not authorized")
        if (
            definition.transport != "environment"
            or not definition.configured
            or not definition.enabled
            or definition.action_policy != "write"
            or not definition.side_effect
        ):
            raise ToolCatalogError("Environment command tools must declare a configured write")
        if (
            not definition.environment_capabilities <= self.lease.definition.capabilities
            or not definition.environment_capabilities <= context.environment_capabilities
        ):
            raise ToolCatalogError("The leased environment cannot support this command")
        prefix = definition.settings.get("argv_prefix")
        extra = arguments.get("args", [])
        if (
            not isinstance(prefix, list)
            or not prefix
            or any(not isinstance(value, str) for value in prefix)
            or not isinstance(extra, list)
            or any(not isinstance(value, str) for value in extra)
            or set(arguments) - {"args"}
        ):
            raise ToolCatalogError("Environment tools require a fixed argv prefix and string args")
        timeout = definition.settings.get("timeout_seconds", self.max_timeout_seconds)
        output_limit = definition.settings.get("max_output_bytes", self.max_output_bytes)
        if (
            type(timeout) is not int
            or timeout < 1
            or type(output_limit) is not int
            or output_limit < 1024
        ):
            raise ToolCatalogError("Environment command limits are invalid")
        try:
            command = ExecutionCommand(
                argv=tuple(prefix + extra),
                timeout_seconds=min(timeout, self.max_timeout_seconds),
                max_output_bytes=min(output_limit, self.max_output_bytes),
            )
        except ValidationError:
            raise ToolCatalogError("Environment command arguments are invalid") from None
        try:
            result = self.manager.execute(
                self.lease.id,
                command,
                attempt_id=assignment.attempt_id,
                fencing_token=self.lease.fencing_token,
            )
        except Exception:
            # Once submitted, assume an uncertain outcome; never retry a command here.
            raise ToolExecutionError("Leased environment command failed", unknown=True) from None
        return result.model_dump(mode="json")
