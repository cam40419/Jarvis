"""Explicit agent bindings to Simon services, with live account and action checks.

These tools never accept host filesystem paths, shell commands, credentials, or
an actor identity from the model. Cloud reads do not provision project folders.
"""

from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic import ValidationError as PydanticError

from simon.adapters.google import ConnectedError
from simon.adapters.tool_transports import ToolHandler
from simon.adapters.workspace_files import WorkspaceFileTransport
from simon.domain.connected_tools import CalendarQuery, GoogleItem, GoogleSearch
from simon.domain.drive import DriveBrowse
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.execution import EnvironmentLease
from simon.domain.models import ActorContext
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)
from simon.services.connected import ConnectedService
from simon.services.identity import ROLE_SCOPES
from simon.services.local_tool_schema import DESCRIPTIONS as LOCAL_DESCRIPTIONS
from simon.services.local_tool_schema import MODELS as LOCAL_MODELS
from simon.services.local_tool_schema import READS as LOCAL_READS

LOCAL_NAMES = (
    "local_files_roots",
    "local_files_list",
    "local_files_search",
    "local_file_read",
    "local_file_write",
    "local_file_edit",
    "local_file_move",
    "local_folder_create",
    "local_zip_inspect",
    "local_zip_extract",
    "local_zip_create",
)
GOOGLE_MODELS: dict[str, type[BaseModel]] = {
    "drive_search_files": GoogleSearch,
    "drive_read_file": GoogleItem,
    "drive_list_folder": DriveBrowse,
    "gmail_search_messages": GoogleSearch,
    "gmail_read_message": GoogleItem,
    "calendar_list_events": CalendarQuery,
}
GOOGLE_DESCRIPTIONS = {
    "google_accounts_list": "List this account's connected Google accounts and permissions.",
    "drive_search_files": "Search Drive files in a connected account, with bounded pagination.",
    "drive_read_file": (
        "Read Drive text or export a Google document, with explicit size limits. "
        "Pass the listing's source_account_id or account_id as the account argument; "
        "do not assume the default account can read another account's files. "
    ),
    "drive_list_folder": "Browse a connected account's Drive folder, with pagination.",
    "gmail_search_messages": "Search messages in a connected Gmail account; read only.",
    "gmail_read_message": "Read a connected Gmail message without downloading attachments.",
    "calendar_list_events": "Read calendar events in a bounded window of at most 31 days.",
}


@lru_cache(maxsize=1)
def _native_templates() -> tuple[ToolDefinition, ...]:
    """Build schemas once; cached models stay private and are never returned to callers."""
    tools = []
    for name in (*LOCAL_NAMES, *GOOGLE_DESCRIPTIONS):
        local = name in LOCAL_NAMES
        cloud = not local
        write = local and name not in LOCAL_READS
        models = LOCAL_MODELS if local else GOOGLE_MODELS
        model = models.get(name)
        schema: dict[str, Any] = (
            model.model_json_schema()
            if model
            else {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
        )
        if local:
            # Workspaces are server-owned account directories, never Desktop/Downloads.
            for field in ("root", "destination_root"):
                if field in schema["properties"]:
                    schema["properties"][field]["pattern"] = r"^workspace$"
        scopes = {"jobs:read"}
        if write:
            scopes.add("jobs:write")
        if not local:
            scopes.add("threads:read")
        descriptions = LOCAL_DESCRIPTIONS if local else GOOGLE_DESCRIPTIONS
        description = descriptions[name]
        if local:
            description += " Agent access is limited to the account workspace."
        tools.append(
            ToolDefinition(
                id=f"native.{name}",
                description=description,
                transport="native",
                categories=frozenset({"files" if local else "google"}),
                capabilities=frozenset({name, "file.write" if write else "file.read"})
                if local
                else frozenset({name}),
                input_schema=schema,
                configured=True,
                required_scopes=frozenset(scopes),
                side_effect=write,
                action_policy="write" if write else "read",
                settings={"network": cloud},
            )
        )
    return tuple(tools)


@lru_cache(maxsize=1)
def _canonical_tools() -> dict[str, ToolDefinition]:
    return {item.id: item for item in _native_templates()}


def _configured(connected: ConnectedService, definition: ToolDefinition) -> bool:
    if definition.id.removeprefix("native.") in LOCAL_NAMES:
        return connected.settings.local_files_enabled
    return connected.configured if definition.settings["network"] else True


def native_tool_definitions(
    connected: ConnectedService | None = None,
) -> tuple[ToolDefinition, ...]:
    """Return owned manifest entries while evaluating current service settings."""
    return tuple(
        item.model_copy(
            deep=True,
            update={
                "configured": connected is None or _configured(connected, item),
            },
        )
        for item in _native_templates()
    )


def bind_native_tools(
    tools: tuple[ToolDefinition, ...],
    connected: ConnectedService,
) -> tuple[ToolDefinition, ...]:
    """Bind declared native IDs to canonical contracts, retaining explicit disabling.

    Native schema/action/scope contracts are code owned. A manifest cannot downgrade
    a write to a read, change a native operation, or invent an executable tool.
    """
    known = {item.id: item for item in native_tool_definitions(connected)}
    definitions = []
    for item in tools:
        if item.transport != "native":
            definitions.append(item)
            continue
        if item.id not in known:
            raise ToolCatalogError(f"Unknown native tool: {item.id}")
        canonical = known[item.id]
        # Cloud handlers are installed before accounts are connected. Account
        # readiness is checked live by native_tool_status and the transport.
        if canonical.settings["network"]:
            canonical = canonical.model_copy(update={"configured": True})
        definitions.append(
            canonical.model_copy(
                update={
                    "enabled": item.enabled,
                    "configured": item.configured and canonical.configured,
                }
            )
        )
    return tuple(definitions)


def native_tool_status(
    connected: ConnectedService,
    actor: ActorContext,
    tool_id: str,
) -> dict[str, Any]:
    definition = _canonical_tools().get(tool_id)
    if definition is None:
        return {"available": False, "reason": "Unknown native tool"}
    if not _configured(connected, definition):
        return {"available": False, "reason": "Set up this service in Connections"}
    if not definition.required_scopes <= actor.scopes:
        return {"available": False, "reason": "Required account permissions are unavailable"}
    name = tool_id.removeprefix("native.")
    if definition.settings["network"]:
        connections = connected.store.google_connections(actor.workspace_id, actor.actor_id)
        if not connections:
            return {"available": False, "reason": "Connect a Google account"}
        ready = name == "google_accounts_list" or name in connected.available(actor)
        if not ready:
            return {"available": False, "reason": "Reconnect Google with this tool's permission"}
    return {"available": True, "reason": None}


class NativeToolTransport:
    def __init__(
        self,
        connected: ConnectedService,
        *,
        actor: ActorContext,
        run_id: UUID,
        revalidate: Callable[[], ActorContext],
    ) -> None:
        self.connected, self.actor, self.run_id = connected, actor, run_id
        self.revalidate = revalidate

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        if (context.actor_id, context.workspace_id, context.run_id) != (
            self.actor.actor_id,
            self.actor.workspace_id,
            self.run_id,
        ):
            raise AuthorizationError("Native tool execution owner changed")
        canonical = _canonical_tools().get(definition.id)
        if canonical is None or definition.transport != "native":
            raise ToolCatalogError("Unknown native tool")
        if (
            not definition.enabled
            or not definition.configured
            or not _configured(self.connected, canonical)
        ):
            raise ToolCatalogError("Native tool is disabled or unconfigured")
        if definition.id not in context.allowed_tool_ids:
            raise AuthorizationError("Native tool was not granted to this assignment")
        if canonical.side_effect and context.authorized_action != "write":
            raise AuthorizationError("Native mutation requires a write-authorized assignment")

        def check() -> ActorContext:
            current = self.revalidate()
            if (current.actor_id, current.workspace_id) != (
                self.actor.actor_id,
                self.actor.workspace_id,
            ):
                raise AuthorizationError("Native tool access changed")
            member = self.connected.identity.membership(current.actor_id, current.workspace_id)
            current = current.model_copy(
                update={
                    "scopes": current.scopes
                    & self.actor.scopes
                    & context.scopes
                    & ROLE_SCOPES[member.role],
                }
            )
            if not canonical.required_scopes <= current.scopes:
                raise AuthorizationError("Native tool permissions changed")
            status = native_tool_status(self.connected, current, definition.id)
            if not status["available"]:
                raise AuthorizationError(str(status["reason"]))
            return current

        actor = check()
        name = definition.id.removeprefix("native.")
        key = f"agent:{context.run_id}:{context.invocation_id}"
        try:
            if name in LOCAL_NAMES:
                result = self._local(actor, name, arguments, key, check)
            else:
                result = self._google(actor, name, arguments, check)
        except (AuthorizationError, ToolCatalogError):
            raise
        except PydanticError:
            if not canonical.side_effect:
                check()
                raise ToolExecutionError("Native read arguments were not accepted.") from None
            raise ValidationError("Invalid native tool arguments") from None
        except (ValidationError, ConnectedError) as error:
            if canonical.side_effect:
                raise
            # A rejected/unsupported read has no uncertain application write.
            # Keep explicit provider uncertainty and live authority checks; never
            # reveal provider bodies, resource content, or raw exception strings.
            check()
            unknown = isinstance(error, ConnectedError) and error.unknown
            raise ToolExecutionError(
                "Native read outcome is uncertain."
                if unknown
                else "Native read could not be completed.",
                unknown=unknown,
            ) from None
        if canonical.side_effect and result.get("status") in {"unknown", "executing"}:
            raise ToolExecutionError("Native operation has an unresolved outcome", unknown=True)
        check()
        return result

    def _local(
        self,
        actor: ActorContext,
        name: str,
        arguments: dict[str, Any],
        key: str,
        check: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        if name == "local_files_roots":
            if arguments:
                raise ValidationError("No arguments expected")
            self.connected.local_files.authorize(actor)
            return {
                "roots": [{"root": "workspace", "name": "My workspace"}],
            }
        for field in ("root", "destination_root"):
            if field not in arguments:
                continue
            root = arguments[field]
            if not isinstance(root, str):
                raise ValidationError("A local root must be a string")
            if root == "workspace":
                continue
            raise AuthorizationError("Agents can only access the account workspace")
        return self.connected.local_files.run(actor, name, arguments, key, check)

    def _google(
        self,
        actor: ActorContext,
        name: str,
        arguments: dict[str, Any],
        check: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        if name == "google_accounts_list":
            if arguments:
                raise ValidationError("No arguments expected")
            return {
                "accounts": [
                    self.connected.account_status(item)
                    for item in self.connected.store.google_connections(
                        actor.workspace_id,
                        actor.actor_id,
                    )
                ]
            }
        request: Any = GOOGLE_MODELS[name].model_validate(arguments)
        if name == "drive_list_folder":
            return self.connected.drive.browse(actor, request, check)
        connection = self.connected.connection(actor, account=request.account)
        self.connected.require_google_scope(connection, name)
        methods: dict[str, Callable[[str, Any], dict[str, Any]]] = {
            "drive_search_files": self.connected.api.drive_search,
            "drive_read_file": self.connected.api.drive_file,
            "gmail_search_messages": self.connected.api.gmail_search,
            "gmail_read_message": self.connected.api.gmail_message,
            "calendar_list_events": self.connected.api.events,
        }
        result = methods[name](self.connected.access_token(actor, connection), request)
        current = check()
        self.connected.require_google_scope(self.connected.connection(current, connection.id), name)
        return {**result, "account_email": connection.email}


def native_transport_factory(
    connected: ConnectedService,
) -> Callable[
    [ActorContext, UUID, Callable[[], ActorContext], EnvironmentLease | None],
    Mapping[str, ToolHandler],
]:
    def factory(
        actor: ActorContext,
        run_id: UUID,
        revalidate: Callable[[], ActorContext],
        lease: EnvironmentLease | None = None,
    ) -> Mapping[str, ToolHandler]:
        handlers: dict[str, ToolHandler] = {
            "native": NativeToolTransport(
                connected,
                actor=actor,
                run_id=run_id,
                revalidate=revalidate,
            )
        }
        if lease is not None and lease.definition.kind == "docker":
            handlers["workspace_files"] = WorkspaceFileTransport(
                connected.local_files,
                lease,
                actor=actor,
                run_id=run_id,
                revalidate=revalidate,
            )
        return handlers

    return factory
