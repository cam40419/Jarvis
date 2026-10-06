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

from simon.adapters.google import DRIVE_WRITE_SCOPE, ConnectedError
from simon.adapters.project_drive import FOLDER
from simon.adapters.tool_transports import ToolHandler
from simon.adapters.workspace_files import WorkspaceFileTransport
from simon.domain.agent_platform import PlatformManifest
from simon.domain.connected_tools import CalendarDraft, CalendarQuery, GoogleItem, GoogleSearch
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.execution import EnvironmentLease
from simon.domain.models import ActorContext
from simon.domain.project_files import DriveBrowse, ProjectDrive
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)
from simon.services.agent_calendar import AgentCalendarService, AgentCalendarStatus
from simon.services.connected import ConnectedService
from simon.services.identity import ROLE_SCOPES
from simon.services.local_tool_schema import DESCRIPTIONS as LOCAL_DESCRIPTIONS
from simon.services.local_tool_schema import MODELS as LOCAL_MODELS
from simon.services.local_tool_schema import READS as LOCAL_READS
from simon.services.project_files import ProjectFileService
from simon.services.project_tool_schema import DESCRIPTIONS as PROJECT_DESCRIPTIONS
from simon.services.project_tool_schema import MODELS as PROJECT_MODELS
from simon.services.project_tool_schema import READS as PROJECT_READS

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
PROJECT_METHODS = {
    "project_files_list": "files",
    "project_file_read": "read",
    "project_sheet_read": "sheet_read",
    "project_file_create": "create_file",
    "project_file_edit": "edit",
    "project_sheet_write": "sheet_write",
    "project_file_rename": "rename",
}
GOOGLE_MODELS: dict[str, type[BaseModel]] = {
    "drive_search_files": GoogleSearch,
    "drive_read_file": GoogleItem,
    "drive_list_folder": DriveBrowse,
    "gmail_search_messages": GoogleSearch,
    "gmail_read_message": GoogleItem,
    "calendar_list_events": CalendarQuery,
    "calendar_create_event": CalendarDraft,
    "calendar_action_status": AgentCalendarStatus,
}
GOOGLE_DESCRIPTIONS = {
    "google_accounts_list": "List this account's connected Google accounts and permissions.",
    "drive_search_files": "Search Drive files in a connected account, with bounded pagination.",
    "drive_read_file": (
        "Read Drive text or export a Google document, with explicit size limits. "
        "Pass the listing's source_account_id or account_id as the account argument; "
        "do not assume the default account can read another account's files. "
        "For files returned by project_files_list, prefer project_file_read with the same "
        "project_id so the project's linked Google account is used."
    ),
    "drive_list_folder": "Browse a connected account's Drive folder, with pagination.",
    "gmail_search_messages": "Search messages in a connected Gmail account; read only.",
    "gmail_read_message": "Read a connected Gmail message without downloading attachments.",
    "calendar_list_events": "Read calendar events in a bounded window of at most 31 days.",
    "calendar_create_event": (
        "Create an event in the selected connected Google account's own primary calendar. "
        "Requires this task's explicit write skill; does not send attendee invitations. "
        "At most three creations per run. Identical drafts in one run/account return the same "
        "receipt. Report succeeded only from its provider receipt; never retry unknown outcomes."
    ),
    "calendar_action_status": "Read a saved calendar creation receipt belonging to this agent run.",
}


@lru_cache(maxsize=1)
def _native_templates() -> tuple[ToolDefinition, ...]:
    """Build schemas once; cached models stay private and are never returned to callers."""
    tools = []
    for name in (*LOCAL_NAMES, "project_list", *PROJECT_METHODS, *GOOGLE_DESCRIPTIONS):
        local = name in LOCAL_NAMES
        project = name == "project_list" or name in PROJECT_METHODS
        cloud = not local and name not in {"project_list", "calendar_action_status"}
        write = (
            (local and name not in LOCAL_READS)
            or (project and name not in PROJECT_READS)
            or name == "calendar_create_event"
        )
        models = LOCAL_MODELS if local else PROJECT_MODELS if project else GOOGLE_MODELS
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
                    schema["properties"][field]["pattern"] = (
                        r"^(workspace|project:[0-9a-fA-F-]{36})$"
                    )
        scopes = {"jobs:read"}
        if write:
            scopes.add("jobs:write")
        if project:
            scopes.add("memories:read")
        if not local and not project:
            scopes.add("threads:read")
        if name == "calendar_create_event":
            scopes.add("threads:write")
        descriptions = (
            LOCAL_DESCRIPTIONS
            if local
            else (PROJECT_DESCRIPTIONS if project else GOOGLE_DESCRIPTIONS)
        )
        description = descriptions[name]
        if local:
            description += " Agent access is limited to workspace and authorized project roots."
        tools.append(
            ToolDefinition(
                id=f"native.{name}",
                description=description,
                transport="native",
                categories=frozenset({"files" if local else "projects" if project else "google"}),
                capabilities=frozenset({name, "file.write" if write else "file.read"})
                if local or project
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


def with_native_tools(
    manifest: PlatformManifest,
    connected: ConnectedService,
) -> PlatformManifest:
    """Bind declared native IDs to canonical contracts, retaining explicit disabling.

    Native schema/action/scope contracts are code owned. A manifest cannot downgrade
    a write to a read, change a native operation, or invent an executable tool.
    """
    known = {item.id: item for item in native_tool_definitions(connected)}
    definitions = []
    for item in manifest.tools:
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
    values = manifest.model_dump(mode="python")
    values["tools"] = definitions
    return PlatformManifest.model_validate(values)


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
        if name in PROJECT_METHODS:
            ready = any(DRIVE_WRITE_SCOPE in item.scopes for item in connections)
        elif name == "google_accounts_list":
            ready = True
        else:
            ready = name in connected.available(actor)
        if not ready:
            return {"available": False, "reason": "Reconnect Google with this tool's permission"}
    return {"available": True, "reason": None}


class _BoundProjectFiles(ProjectFileService):
    """Reuse project operations without their chat-only automatic provisioning."""

    def context(self, actor: ActorContext, project_id: UUID) -> tuple[str, ProjectDrive]:
        self.project(actor, project_id)
        binding = self.binding(actor, project_id)
        if not binding.enabled or not binding.folder_id or not binding.google_email:
            raise ValidationError("Link a project Drive folder in Files before using this tool.")
        token, email = self.access(actor, project_id)
        if email != binding.google_email:
            raise AuthorizationError("Project Google account changed")
        if self.api.metadata(token, binding.folder_id)["mimeType"] != FOLDER:
            raise ValidationError("The linked project location is not a folder")
        self.assert_binding(actor, binding)
        return token, binding


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
            elif name == "project_list":
                if arguments:
                    raise ValidationError("No arguments expected")
                result = {"projects": self.connected.projects.list(actor)}
            elif name in PROJECT_METHODS:
                request = PROJECT_MODELS[name].model_validate(arguments)
                projects = _BoundProjectFiles(self.connected)
                projects.api = self.connected.projects.api
                method = getattr(projects, PROJECT_METHODS[name])
                result = (
                    method(actor, request, key, check)
                    if canonical.side_effect
                    else (method(actor, request))
                )
            elif name == "calendar_create_event":
                receipt = AgentCalendarService(self.connected).create(
                    actor,
                    self.run_id,
                    context.agent_id,
                    context.invocation_id,
                    CalendarDraft.model_validate(arguments),
                    check,
                )
                result = {
                    **receipt.model_dump(mode="json"),
                    "created": receipt.status == "succeeded",
                    "requires_confirmation": False,
                }
            elif name == "calendar_action_status":
                request = AgentCalendarStatus.model_validate(arguments)
                receipt = AgentCalendarService(self.connected).get(
                    actor,
                    self.run_id,
                    request.action_id,
                )
                result = receipt.model_dump(mode="json")
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
                "note": "Authorized projects use root project:<project_id>; list projects first.",
            }
        for field in ("root", "destination_root"):
            if field not in arguments:
                continue
            root = arguments[field]
            if not isinstance(root, str):
                raise ValidationError("A local root must be a string")
            if root == "workspace":
                continue
            if not root.startswith("project:"):
                raise AuthorizationError("Agents can only access workspace and project roots")
            try:
                identifier = UUID(root.partition(":")[2])
            except ValueError:
                raise ValidationError("Invalid project root") from None
            self.connected.projects.project(actor, identifier)
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
            return self.connected.projects.browse(actor, request, check)
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
