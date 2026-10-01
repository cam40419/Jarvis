"""Static browser reads/screenshots with explicit destinations and leased isolation."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from simon.adapters._browser_runner import OPERATIONS, allowed_origins, origin, validate_arguments
from simon.adapters.environment_tools import LeasedCommandExecutor
from simon.domain.errors import AuthorizationError
from simon.domain.execution import EnvironmentLease, ExecutionCommand
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)

BROWSER_CAPABILITIES = frozenset({"browser", "python"})


@lru_cache(maxsize=1)
def _runner_source() -> str:
    return Path(__file__).with_name("_browser_runner.py").read_text(encoding="utf-8")


def browser_tool_definitions(*, enabled: bool = False) -> tuple[ToolDefinition, ...]:
    path = {"type": "string", "minLength": 1, "maxLength": 500}
    descriptions = {
        "browser.read": "Read text/title from an allowed public HTTPS page with scripts disabled.",
        "browser.screenshot": "Read an allowed HTTPS page and save a new 1365 by 900 PNG image.",
        "browser.render_html": "Render local HTML to a new PNG with scripts and network disabled.",
    }
    definitions = []
    for operation in sorted(OPERATIONS):
        local, write = operation == "browser.render_html", operation != "browser.read"
        properties = {
            ("input" if local else "url"): path
            if local
            else {
                "type": "string",
                "minLength": 1,
                "maxLength": 4000,
            },
            "max_text_chars": {"type": "integer", "minimum": 100, "maximum": 16000},
        }
        required = ["input" if local else "url"]
        if write:
            properties["output"] = path
            required.append("output")
        definitions.append(
            ToolDefinition(
                id=operation,
                description=descriptions[operation],
                transport="browser",
                categories=frozenset({"web"}),
                capabilities=frozenset({operation}),
                enabled=enabled,
                configured=enabled,
                required_scopes=frozenset({"jobs:write" if write else "jobs:read"}),
                environment_capabilities=BROWSER_CAPABILITIES,
                side_effect=write,
                action_policy="write" if write else "read",
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": properties,
                    "required": required,
                },
                output_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "exit_code": {"type": "integer"},
                        "stdout": {"type": "string"},
                        "stderr": {"type": "string"},
                        "truncated": {"type": "boolean"},
                    },
                    "required": ["exit_code", "stdout", "stderr", "truncated"],
                },
                settings={"allowed_origins": []},
            )
        )
    return tuple(definitions)


class BrowserToolTransport:
    def __init__(
        self,
        manager: LeasedCommandExecutor,
        lease: EnvironmentLease,
        *,
        actor_id: UUID,
        run_id: UUID,
        max_timeout_seconds: int = 60,
        max_output_bytes: int = 131072,
    ) -> None:
        if (
            type(max_timeout_seconds) is not int
            or not 1 <= max_timeout_seconds <= 600
            or type(max_output_bytes) is not int
            or not 1024 <= max_output_bytes <= 1048576
        ):
            raise ValueError("Browser transport limits are outside their supported range")
        if lease.status != "active":
            raise ToolCatalogError("Browser tools require an active lease")
        self.manager, self.lease = manager, lease.model_copy(deep=True)
        self.actor_id, self.run_id = actor_id, run_id
        self.max_timeout_seconds, self.max_output_bytes = max_timeout_seconds, max_output_bytes

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        assignment = self.lease.plan.request
        if (
            context.actor_id != self.actor_id
            or context.household_id != assignment.workspace_id
            or context.run_id != self.run_id
            or context.agent_id != assignment.agent_id
        ):
            raise AuthorizationError("Browser operation does not belong to this worker lease")
        operation = definition.id
        if operation not in OPERATIONS:
            raise ToolCatalogError("Unsupported browser operation")
        write = operation != "browser.read"
        if (
            definition.id not in context.allowed_tool_ids
            or ("jobs:write" if write else "jobs:read") not in context.scopes
            or not definition.required_scopes <= context.scopes
            or (write and context.authorized_action not in {"write", "external_commitment"})
        ):
            raise AuthorizationError("Browser operation was not authorized")
        if (
            definition.transport != "browser"
            or not definition.enabled
            or not definition.configured
            or definition.side_effect != write
            or definition.action_policy != ("write" if write else "read")
        ):
            raise ToolCatalogError("Browser tool declarations do not match its operation")
        network = "none" if operation == "browser.render_html" else "bridge"
        if (
            self.lease.definition.kind != "docker"
            or self.lease.definition.os != "linux"
            or self.lease.plan.network != network
            or self.lease.definition.network != network
            or not self.lease.definition.capabilities >= BROWSER_CAPABILITIES
            or not context.environment_capabilities >= BROWSER_CAPABILITIES
            or not definition.environment_capabilities <= self.lease.definition.capabilities
            or not definition.environment_capabilities <= context.environment_capabilities
        ):
            raise ToolCatalogError("Browser tool requires its configured Docker/network capability")
        try:
            checked = validate_arguments(operation, arguments)
            origins = allowed_origins(definition.settings.get("allowed_origins", []))
            if operation != "browser.render_html" and origin(checked["url"]) not in origins:
                raise ValueError("URL is outside the operator-configured origins")
        except ValueError as error:
            raise ToolCatalogError(str(error)) from None
        command = ExecutionCommand(
            argv=(
                "/usr/local/bin/python3",
                "-I",
                "-c",
                _runner_source(),
                json.dumps(
                    {
                        "operation": operation,
                        "arguments": checked,
                        "allowed_origins": sorted(origins),
                        "timeout_seconds": max(1, self.max_timeout_seconds - 2),
                    }
                ),
            ),
            timeout_seconds=self.max_timeout_seconds,
            max_output_bytes=self.max_output_bytes,
        )
        try:
            result = self.manager.execute(
                self.lease.id,
                command,
                attempt_id=assignment.attempt_id,
                fencing_token=self.lease.fencing_token,
            )
        except Exception:
            raise ToolExecutionError("Leased browser command failed", unknown=write) from None
        return result.model_dump(mode="json")
