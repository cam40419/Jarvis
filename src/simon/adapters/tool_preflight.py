"""Installed transport capabilities and offline integration configuration checks."""

from collections.abc import Mapping

from simon.adapters._browser_runner import allowed_origins
from simon.adapters.cad_tools import cad_configuration_reason
from simon.adapters.cloud_storage_tools import cloud_storage_tool_status
from simon.adapters.generative_tools import generative_configuration_reason
from simon.adapters.optional_http import optional_tool_status
from simon.adapters.pcb_tools import pcb_configuration_reason
from simon.domain.models import ActorContext
from simon.domain.tool_catalog import ToolDefinition

INSTALLED_TRANSPORTS = (
    "http", "environment", "native", "git", "mcp", "workspace_files", "processing",
    "github", "webdav", "dropbox", "box", "onedrive", "browser", "generative", "cad", "pcb",
    "project_work", "external_actions",
)


def uses_network(tool: ToolDefinition) -> bool:
    """Transport identity wins over a mistakenly omitted network declaration."""
    return (
        tool.transport in {
            "http", "mcp", "github", "webdav", "dropbox", "box", "onedrive", "generative",
        }
        or (tool.transport == "browser" and tool.id != "browser.render_html")
        or (tool.transport == "external_actions" and tool.id == "external_actions.quote")
        or tool.settings.get("network") is True
    )


def integration_status(
    tool: ToolDefinition, actor: ActorContext, environ: Mapping[str, str],
) -> tuple[str, tuple[str, ...]]:
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
        not tool.endpoint or not isinstance(tool.settings.get("tool_name"), str)
        or not tool.settings.get("tool_name")
        or tool.settings.get("protocol_version", "2025-11-25") != "2025-11-25"
    ):
        return "unconfigured", ("Configure an MCP endpoint, tool name and supported protocol",)
    if tool.transport == "browser":
        if tool.id not in {"browser.read", "browser.screenshot", "browser.render_html"}:
            return "unconfigured", ("Unknown browser operation",)
        try:
            origins = allowed_origins(tool.settings.get("allowed_origins", []))
        except ValueError:
            return "unconfigured", ("Configure exact HTTPS origins for browser access",)
        if tool.id != "browser.render_html" and not origins:
            return "unconfigured", ("Configure exact HTTPS origins for browser access",)
    return "configured", ()
