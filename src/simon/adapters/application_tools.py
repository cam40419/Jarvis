"""Structured calls to operator-installed application bridges in an owned machine lease."""

import json
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from simon.adapters.environment_tools import EnvironmentCommandTransport, LeasedCommandExecutor
from simon.creative_host import EXTENSIONS
from simon.domain.execution import EnvironmentLease
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)


def application_configuration_reason(tool: ToolDefinition) -> str | None:
    if tool.transport != "application":
        return "Unknown application transport"
    prefix = tool.settings.get("argv_prefix")
    if (
        not isinstance(prefix, list)
        or not prefix
        or any(not isinstance(item, str) or not item or "\x00" in item for item in prefix)
        or not isinstance(tool.settings.get("operation"), str)
        or not tool.settings.get("operation")
        or not isinstance(tool.settings.get("application"), str)
        or not tool.settings.get("application")
        or not {"application.control"} <= tool.environment_capabilities
        or not {"jobs:write"} <= tool.required_scopes
        or not tool.side_effect
        or tool.action_policy != "write"
    ):
        return "Application tools require a fixed bridge, operation, application, and write grant"
    return None


class ApplicationToolTransport:
    """The bridge receives JSON data, never model-selected executables or shell text.

    Bridges own application-specific validation, native save/export, and result receipts.
    A desktop bridge can observe a UI without changing its document, but remains a write
    capability because attaching/focusing an interactive session affects that session.
    """

    def __init__(
        self,
        manager: LeasedCommandExecutor,
        lease: EnvironmentLease,
        *,
        actor_id: UUID,
        run_id: UUID,
        max_timeout_seconds: int = 60,
    ) -> None:
        self.lease = lease.model_copy(deep=True)
        self.commands = EnvironmentCommandTransport(
            manager,
            lease,
            actor_id=actor_id,
            run_id=run_id,
            max_timeout_seconds=max_timeout_seconds,
        )

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        reason = application_configuration_reason(definition)
        if reason:
            raise ToolCatalogError(reason)
        if self.lease.definition.kind != "machine":
            raise ToolCatalogError("Application bridges require a dedicated machine lease")
        if (
            definition.settings["application"] == "windows-uia"
            and self.lease.definition.os != "windows"
        ):
            raise ToolCatalogError("Windows UI Automation requires a Windows machine")
        try:
            Draft202012Validator(definition.input_schema).validate(arguments)
        except ValidationError:
            raise ToolCatalogError(
                "Application arguments do not match the granted operation"
            ) from None
        payload = json.dumps(
            {
                "version": 1,
                "operation": definition.settings["operation"],
                "arguments": arguments,
                "invocation_id": str(context.invocation_id),
                "lease_id": str(self.lease.id),
                "fencing_token": self.lease.fencing_token,
            },
            ensure_ascii=True,
            allow_nan=False,
        )
        if len(payload) > 64000:
            raise ToolCatalogError("Application request exceeds the bridge input limit")
        result = self.commands(
            definition.model_copy(update={"transport": "environment"}),
            {"args": [payload]},
            context,
        )
        if result["exit_code"] != 0:
            raise ToolExecutionError(
                "Application bridge failed; inspect before retrying", unknown=True
            )
        return result


def desktop_tool_definitions(
    *,
    python_executable: str,
    enabled: bool = False,
) -> tuple[ToolDefinition, ...]:
    identity = {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 16}
    return tuple(
        ToolDefinition(
            id="desktop." + operation,
            description=description,
            transport="application",
            categories=frozenset({"desktop", "applications"}),
            enabled=enabled,
            configured=enabled,
            required_scopes=frozenset({"jobs:write"}),
            environment_capabilities=frozenset({"application.control", "desktop.windows.uia"}),
            side_effect=True,
            action_policy="write",
            settings={
                "application": "windows-uia",
                "operation": operation,
                "network": True,
                "argv_prefix": [python_executable, "-m", "simon.desktop_bridge"],
            },
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {}
                if operation == "inspect"
                else {
                    "control_id": identity,
                    "expected_state": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                    **(
                        {"text": {"type": "string", "maxLength": 4000}}
                        if operation == "set_text"
                        else {}
                    ),
                },
                "required": []
                if operation == "inspect"
                else [
                    "control_id",
                    "expected_state",
                    *(["text"] if operation == "set_text" else []),
                ],
            },
        )
        for operation, description in (
            (
                "inspect",
                "Inspect accessible controls in the operator-selected Windows application.",
            ),
            (
                "invoke",
                "Invoke an accessible control using the exact state returned by desktop.inspect.",
            ),
            (
                "set_text",
                "Set an editable control's text using the exact state from desktop.inspect.",
            ),
        )
    )


def creative_tool_definitions(
    *, python_executable: str, application: str, enabled: bool = False
) -> tuple[ToolDefinition, ...]:
    """Opt-in native operations; each application needs its own runner-owned mailbox."""
    if application not in EXTENSIONS:
        raise ValueError("Unknown creative application")
    schemas: dict[str, dict[str, Any]] = {
        "inspect": {},
        "save": {
            "output": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "pattern": r"^[A-Za-z0-9_-]+\.("
                + "|".join(extension[1:] for extension in EXTENSIONS[application])
                + r")$",
            }
        },
    }
    if application == "fusion":
        schemas["set_parameter"] = {
            "name": {"type": "string", "minLength": 1, "maxLength": 1000},
            "expression": {"type": "string", "minLength": 1, "maxLength": 1000},
        }
    elif application == "houdini":
        schemas["set_parameter"] = {
            "name": {"type": "string", "pattern": "^/obj/", "maxLength": 1000},
            "value": {"type": "number"},
        }
    return tuple(
        ToolDefinition(
            id=f"{application}.{operation}",
            description=(
                f"{operation.replace('_', ' ').capitalize()} in the owned {application} session."
            ),
            transport="application",
            categories=frozenset({"applications", "creative"}),
            enabled=enabled,
            configured=enabled,
            required_scopes=frozenset({"jobs:write"}),
            environment_capabilities=frozenset(
                {"application.control", f"application.{application}"}
            ),
            side_effect=True,
            action_policy="write",
            settings={
                "application": application,
                "operation": operation,
                "network": True,
                "argv_prefix": [python_executable, "-m", "simon.creative_bridge", application],
            },
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": list(properties),
            },
        )
        for operation, properties in schemas.items()
    )
