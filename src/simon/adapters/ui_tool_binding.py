"""Bind stock integration templates to backend connections without editing manifests."""

from simon.adapters.browser_tools import browser_tool_definitions
from simon.adapters.cloud_storage_tools import (
    box_tool_definitions,
    dropbox_tool_definitions,
    onedrive_tool_definitions,
)
from simon.adapters.github_tools import github_tool_definitions
from simon.adapters.webdav_tools import webdav_tool_definitions
from simon.domain.agent_platform import PlatformManifest
from simon.services.tool_catalog import builtin_tool_templates


def with_ui_tools(manifest: PlatformManifest) -> PlatformManifest:
    # Older starter manifests included extension examples as installed tools. Remove
    # only untouched examples; configured or customized operator contracts survive.
    templates = {tool.id: tool for tool in builtin_tool_templates()}
    github = {tool.id: tool for tool in github_tool_definitions(enabled=True)}
    storage = {
        tool.id: tool
        for factory in (
            box_tool_definitions,
            dropbox_tool_definitions,
            onedrive_tool_definitions,
            webdav_tool_definitions,
        )
        for tool in factory(enabled=True)
    }
    screenshot = next(
        tool for tool in browser_tool_definitions() if tool.id == "browser.screenshot"
    )
    reader = next((tool for tool in manifest.tools if tool.id == "browser.read"), None)
    tools = []
    for tool in manifest.tools:
        if tool == templates.get(tool.id):
            continue
        if (
            tool.id in github
            and not tool.settings.get("repositories")
            and not tool.settings.get("workspace_id")
        ):
            canonical = github[tool.id]
            tool = canonical.model_copy(
                update={
                    "enabled": True,
                    "configured": True,
                    "credential_env": None,
                    "settings": {"ui_managed": True, "network": True},
                }
            )
        elif tool.id in storage and not tool.settings.get("workspace_id"):
            tool = storage[tool.id].model_copy(
                update={
                    "enabled": True,
                    "configured": True,
                    "credential_env": None,
                    "settings": {"ui_managed": True, "network": True},
                }
            )
        elif (
            tool.id == "browser.screenshot"
            and not tool.enabled
            and not tool.configured
            and not tool.settings.get("allowed_origins")
            and reader
            and reader.enabled
            and reader.configured
        ):
            tool = screenshot.model_copy(
                update={"enabled": True, "configured": True, "settings": dict(reader.settings)}
            )
        tools.append(tool)
    return PlatformManifest.model_validate({**manifest.model_dump(mode="python"), "tools": tools})
