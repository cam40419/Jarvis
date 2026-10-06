"""Static browser reads/screenshots with explicit destinations and leased isolation."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError

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


def browser_read_output_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "title": {"type": "string", "maxLength": 1000},
        "url": {"type": "string", "minLength": 1, "maxLength": 4000},
        "status": {"type": "integer", "minimum": 200, "maximum": 299},
        "text": {"type": "string", "maxLength": 16000},
        "text_truncated": {"type": "boolean"},
        "blocked_requests": {"type": "integer", "minimum": 0},
        "links": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "url": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "title": {"type": "string", "maxLength": 240},
                },
                "required": ["url", "title"],
            },
        },
        "links_truncated": {"type": "boolean"},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate browser result field")
        result[key] = value
    return result


def _read_output(result: Any, max_text_chars: int) -> dict[str, Any]:
    try:
        if result.truncated or len(result.stdout.encode("utf-8")) > 131072:
            raise ValueError("Truncated browser result")
        page = json.loads(result.stdout, object_pairs_hook=_unique_fields)
        # The runner also handles screenshots; read results never publish a path.
        if not isinstance(page, dict) or page.pop("screenshot_path", None) is not None:
            raise ValueError("Invalid browser page")
        Draft202012Validator(browser_read_output_schema()).validate(page)
        origin(page["url"])
        for link in page["links"]:
            origin(link["url"])
        if (
            len(page["text"]) > max_text_chars
            or len(json.dumps(page["links"], ensure_ascii=False).encode("utf-8")) > 16000
        ):
            raise ValueError("Browser result exceeds its bounded fields")
        return page
    except (ValueError, TypeError, RecursionError, UnicodeError, SchemaValidationError):
        raise ToolExecutionError(
            "The browser page result was incomplete or invalid; its contents were not verified.",
            unknown=False,
        ) from None


@lru_cache(maxsize=1)
def _runner_source() -> str:
    return Path(__file__).with_name("_browser_runner.py").read_text(encoding="utf-8")


def browser_tool_definitions(*, enabled: bool = False) -> tuple[ToolDefinition, ...]:
    path = {"type": "string", "minLength": 1, "maxLength": 500}
    descriptions = {
        "browser.read": (
            "Read text, title, source URL and outgoing links from a public HTTPS page with "
            "scripts disabled. Destinations follow the server's exact-origin or public-web "
            "policy. This reads known pages; use a granted web-search tool to discover sources."
            " Successful reads return source URL/title and text directly. Reuse that evidence; "
            "if the worker context compacted it, retrieve the saved /output/text instead of "
            "rereading the same page. Repeat only for a changed or incomplete source."
        ),
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
                capabilities=frozenset({operation, "web.read"})
                if operation == "browser.read"
                else frozenset({operation}),
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
                output_schema=browser_read_output_schema()
                if operation == "browser.read"
                else {
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
                settings={"allowed_origins": [], "public_web": False},
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
            or context.workspace_id != assignment.workspace_id
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
            public_web = definition.settings.get("public_web", False)
            if type(public_web) is not bool:
                raise ValueError("Public-web access must be explicitly configured as a boolean")
            if (
                operation != "browser.render_html"
                and not public_web
                and origin(checked["url"]) not in origins
            ):
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
                        "public_web": public_web,
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
        if operation == "browser.read" and result.exit_code != 0:
            raise ToolExecutionError(
                "The public page could not be read or was blocked by its destination/resource "
                "policy. Try another source; this result does not verify the page's contents.",
                unknown=False,
            )
        if operation == "browser.read":
            return _read_output(result, checked["max_text_chars"])
        return result.model_dump(mode="json")
