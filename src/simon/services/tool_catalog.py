"""Tool discovery and explicit permission checks, independent of tool providers."""

from __future__ import annotations

from collections.abc import Iterable

from simon.domain.errors import AuthorizationError
from simon.domain.tool_catalog import ToolCatalogError, ToolDefinition


class ToolCatalog:
    def __init__(
        self,
        definitions: Iterable[ToolDefinition] = (),
        *,
        available_transports: Iterable[str] | None = None,
    ) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._transports = (
            frozenset(available_transports) if available_transports is not None else None
        )
        for definition in definitions:
            if definition.id in self._definitions:
                raise ToolCatalogError(f"Duplicate tool definition: {definition.id}")
            # Take an owned copy: callers cannot mutate schemas after catalog validation.
            self._definitions[definition.id] = ToolDefinition.model_validate(
                definition.model_dump(mode="python")
            )

    def discover(
        self,
        *,
        query: str = "",
        categories: Iterable[str] = (),
        capabilities: Iterable[str] = (),
        scopes: Iterable[str] = (),
    ) -> tuple[ToolDefinition, ...]:
        category_filter, capability_filter = set(categories), set(capabilities)
        granted_scopes = frozenset(scopes)
        query = query.casefold().strip()
        return tuple(
            item.model_copy(deep=True)
            for item in self._definitions.values()
            if item.enabled
            and item.configured
            and item.required_scopes <= granted_scopes
            and (self._transports is None or item.transport in self._transports)
            and (not category_filter or category_filter & item.categories)
            and capability_filter <= item.capabilities
            and (
                not query
                or query in " ".join(
                    (item.id, item.description, *item.categories, *item.capabilities)
                ).casefold()
            )
        )

    def resolve(
        self,
        ids: Iterable[str],
        *,
        capabilities: Iterable[str] = (),
        scopes: Iterable[str] = (),
        environment_capabilities: Iterable[str] = (),
    ) -> tuple[ToolDefinition, ...]:
        selected: list[ToolDefinition] = []
        granted_scopes, environment = frozenset(scopes), frozenset(environment_capabilities)
        for tool_id in dict.fromkeys(ids):
            item = self._definitions.get(tool_id)
            if item is None:
                raise ToolCatalogError(f"Unknown tool: {tool_id}")
            if not item.required_scopes <= granted_scopes:
                raise AuthorizationError(f"Missing permission for tool: {tool_id}")
            if not item.enabled or not item.configured:
                raise ToolCatalogError(f"Tool is disabled or unconfigured: {tool_id}")
            if self._transports is not None and item.transport not in self._transports:
                raise ToolCatalogError(f"Tool has no execution handler: {tool_id}")
            if not item.environment_capabilities <= environment:
                raise ToolCatalogError(f"Execution environment cannot support tool: {tool_id}")
            selected.append(item.model_copy(deep=True))
        supplied = frozenset(capability for item in selected for capability in item.capabilities)
        if not frozenset(capabilities) <= supplied:
            raise ToolCatalogError("The selected tools do not supply all requested capabilities")
        return tuple(selected)


def builtin_tool_templates() -> tuple[ToolDefinition, ...]:
    """Configuration starting points, never advertised as installed or granted tools."""
    rows = (
        ("image.generate", "image", "image.generate", "http", ""),
        ("image.edit", "image", "image.edit", "http", ""),
        ("video.generate", "video", "video.generate", "external_job", ""),
        ("video.edit", "video", "video.edit", "cli", "video.editor"),
        ("audio.produce", "audio", "audio.produce", "external_job", ""),
        ("graphics.design", "graphics", "graphics.edit", "desktop", "graphics.editor"),
        ("cad.design", "cad", "cad.edit", "desktop", "cad.application"),
        ("pcb.design", "pcb", "pcb.edit", "desktop", "pcb.application"),
        ("three_d.design", "3d", "3d.edit", "desktop", "3d.application"),
        ("render.submit", "render", "render.submit", "external_job", "render.engine"),
        ("simulation.submit", "simulation", "simulation.submit", "external_job", "simulation"),
        ("writing.create", "writing", "document.create", "python", ""),
        ("files.create", "files", "file.write", "python", "workspace.write"),
        ("files.read", "files", "file.read", "python", "workspace.read"),
        ("storage.manage", "storage", "storage.write", "mcp", ""),
        ("web.research", "web", "web.read", "http", "network"),
        ("browser.interact", "web", "browser.control", "browser", "browser"),
        ("desktop.control", "computer", "desktop.control", "desktop", "desktop"),
        ("data.query", "data", "data.read", "mcp", ""),
        ("data.manage", "data", "data.write", "mcp", ""),
        ("code.execute", "software", "code.execute", "cli", "process.execute"),
        ("home.manage", "home", "home.control", "http", "network"),
    )
    read_only = {"files.read", "web.research", "data.query"}
    return tuple(
        ToolDefinition(
            id=tool_id,
            description=f"Configure a provider or application for {tool_id}.",
            categories=frozenset({category}),
            capabilities=frozenset({capability}),
            transport=transport,
            enabled=False,
            configured=False,
            required_scopes=frozenset({f"tools:{tool_id}"}),
            environment_capabilities=frozenset({environment}) if environment else frozenset(),
            side_effect=tool_id not in read_only,
            action_policy="read" if tool_id in read_only else "write",
        )
        for tool_id, category, capability, transport, environment in rows
    )
