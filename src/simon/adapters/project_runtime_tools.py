"""Expose installed project tools without modifying any agent's saved grants."""

from simon.adapters.project_board_tools import project_board_tool_definitions
from simon.adapters.project_journal_tools import project_journal_definitions
from simon.adapters.project_storage_tools import project_storage_definitions
from simon.adapters.project_work_tools import project_work_tool_definitions
from simon.domain.agent_platform import PlatformManifest


def with_project_runtime_tools(manifest: PlatformManifest) -> PlatformManifest:
    if not manifest.agents:
        return manifest
    known = {tool.id for tool in manifest.tools}
    additions = tuple(
        tool
        for tool in (
            *project_work_tool_definitions(),
            *project_journal_definitions(),
            *project_board_tool_definitions(),
            *project_storage_definitions(),
        )
        if tool.id not in known
    )
    return PlatformManifest.model_validate(
        {
            **manifest.model_dump(),
            "tools": (*manifest.tools, *additions),
        }
    )
