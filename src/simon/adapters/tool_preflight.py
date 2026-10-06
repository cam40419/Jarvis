"""Installed transport capabilities and offline integration configuration checks."""

from collections.abc import Mapping

from simon.adapters._browser_runner import allowed_origins
from simon.adapters.application_tools import application_configuration_reason
from simon.adapters.cad_tools import cad_configuration_reason
from simon.adapters.cloud_storage_tools import cloud_storage_tool_status
from simon.adapters.generative_tools import generative_configuration_reason
from simon.adapters.optional_http import optional_tool_status
from simon.adapters.pcb_tools import pcb_configuration_reason
from simon.adapters.web_research import web_search_status
from simon.domain.models import ActorContext
from simon.domain.tool_catalog import ToolDefinition

INSTALLED_TRANSPORTS = (
    "application",
    "http",
    "environment",
    "native",
    "git",
    "mcp",
    "workspace_files",
    "processing",
    "github",
    "webdav",
    "dropbox",
    "box",
    "onedrive",
    "browser",
    "web_research",
    "generative",
    "cad",
    "pcb",
    "project_work",
    "external_actions",
    "project_outputs",
    "project_journal",
    "project_boards",
    "project_storage",
)


def uses_network(tool: ToolDefinition) -> bool:
    """Transport identity wins over a mistakenly omitted network declaration."""
    return (
        tool.transport
        in {
            "http",
            "mcp",
            "github",
            "webdav",
            "dropbox",
            "box",
            "onedrive",
            "generative",
            "web_research",
            "application",
        }
        or (tool.transport == "browser" and tool.id != "browser.render_html")
        or (tool.transport == "external_actions" and tool.id == "external_actions.quote")
        or (tool.transport == "project_boards" and tool.id != "clickup.project_read")
        or tool.settings.get("network") is True
    )


def integration_status(
    tool: ToolDefinition,
    actor: ActorContext,
    environ: Mapping[str, str],
) -> tuple[str, tuple[str, ...]]:
    if tool.transport == "web_research":
        return web_search_status(tool, actor, environ)
    if tool.transport == "application" and (reason := application_configuration_reason(tool)):
        return "unconfigured", (reason,)
    if tool.transport == "project_outputs":
        from simon.adapters.project_output_tools import project_output_configuration_reason

        if reason := project_output_configuration_reason(tool):
            return "unconfigured", (reason,)
    if tool.transport == "project_journal":
        from simon.adapters.project_journal_tools import project_journal_configuration_reason

        if reason := project_journal_configuration_reason(tool):
            return "unconfigured", (reason,)
    if tool.transport == "project_boards":
        from simon.adapters.project_board_tools import project_board_tool_configuration_reason

        if reason := project_board_tool_configuration_reason(tool):
            return "unconfigured", (reason,)
    if tool.transport == "workspace_files":
        from simon.adapters.workspace_files import workspace_file_configuration_reason

        if reason := workspace_file_configuration_reason(tool):
            return "unconfigured", (reason,)
    if tool.transport == "external_actions":
        from simon.adapters.external_action_tools import external_action_configuration_reason

        if reason := external_action_configuration_reason(tool):
            return "unconfigured", (reason,)
    if tool.transport == "project_work":
        from simon.adapters.project_work_tools import project_tool_configuration_reason

        if reason := project_tool_configuration_reason(tool):
            return "unconfigured", (reason,)
    if tool.transport in {"github", "webdav"}:
        return optional_tool_status(tool, actor, environ)
    if tool.transport in {"dropbox", "box", "onedrive"}:
        return cloud_storage_tool_status(tool, actor, environ)
    if tool.transport == "generative":
        reason = generative_configuration_reason(tool)
        if reason:
            return "unconfigured", (reason,)
    if tool.transport == "cad":
        reason = cad_configuration_reason(tool)
        if reason:
            return "unconfigured", (reason,)
    if tool.transport == "pcb":
        reason = pcb_configuration_reason(tool)
        if reason:
            return "unconfigured", (reason,)
    if tool.transport == "mcp" and (
        not tool.endpoint
        or not isinstance(tool.settings.get("tool_name"), str)
        or not tool.settings.get("tool_name")
        or tool.settings.get("protocol_version", "2025-11-25") != "2025-11-25"
    ):
        return "unconfigured", ("Configure an MCP endpoint, tool name and supported protocol",)
    if tool.transport == "browser":
        if tool.id not in {"browser.read", "browser.screenshot", "browser.render_html"}:
            return "unconfigured", ("Unknown browser operation",)
        if tool.id == "browser.read" and "stdout" in tool.output_schema.get("required", []):
            return "unconfigured", (
                "Update browser.read from the current tool template: page reads now return "
                "structured text and source metadata instead of a command-output envelope",
            )
        try:
            origins = allowed_origins(tool.settings.get("allowed_origins", []))
        except ValueError:
            return "unconfigured", ("Configure exact HTTPS origins for browser access",)
        if (
            tool.id != "browser.render_html"
            and not origins
            and tool.settings.get("public_web") is not True
        ):
            return "unconfigured", ("Configure exact HTTPS origins for browser access",)
    return "configured", ()
