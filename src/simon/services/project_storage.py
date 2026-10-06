"""Persist file locations and reuse bounded provider adapters inside project roots."""

from collections.abc import Callable
from typing import Any, cast
from urllib.parse import quote
from uuid import UUID

from simon.adapters.cloud_storage_tools import BoxTransport, DropboxTransport, OneDriveTransport
from simon.adapters.google import DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE
from simon.adapters.optional_http import relative_path
from simon.adapters.project_drive import FOLDER
from simon.adapters.webdav_tools import WebDAVTransport
from simon.domain.errors import (
    AuthorizationError,
    DomainError,
    InvalidTransitionError,
    ValidationError,
)
from simon.domain.integrations import IntegrationConnection
from simon.domain.local_files import LocalList, LocalRead
from simon.domain.models import ActorContext, Channel, StrictModel
from simon.domain.project_files import (
    ProjectDrive,
    ProjectFile,
    ProjectFileCreate,
    ProjectFileEdit,
    ProjectFileRename,
    ProjectFiles,
    ProjectSheetRead,
    ProjectSheetWrite,
)
from simon.domain.project_storage import ProjectStorageLocation, SaveProjectStorage
from simon.domain.tool_catalog import ToolDefinition, ToolExecutionContext
from simon.services.canonical import digest
from simon.services.connected import ConnectedService
from simon.services.local_files import parts
from simon.services.project_files import ProjectFileService
from simon.services.project_work import ProjectWorkService
from simon.services.project_workspace import ProjectWorkspaceService

STORAGE_TRANSPORTS = frozenset({"box", "dropbox", "onedrive", "webdav"})


class _LocationDrive(ProjectFileService):
    def __init__(
        self,
        storage: "ProjectStorageService",
        actor: ActorContext,
        project_id: UUID,
        location: ProjectStorageLocation,
    ) -> None:
        super().__init__(storage.connected)
        self.storage, self.location, self.project_id = storage, location, project_id
        self.api = storage.connected.projects.api

    def binding(self, actor: ActorContext, project_id: UUID) -> ProjectDrive:
        if project_id != self.project_id:
            raise AuthorizationError("Storage belongs to another project")
        location = self.storage.resolved(actor, project_id, self.location)
        return ProjectDrive(
            project_id=project_id,
            workspace_id=actor.workspace_id,
            actor_id=actor.actor_id,
            folder_id=location.folder_id,
            google_email=location.account,
            status="ready",
        )

    def context(self, actor: ActorContext, project_id: UUID) -> tuple[str, ProjectDrive]:
        self.project(actor, project_id)
        binding = self.binding(actor, project_id)
        token, _ = self.access(actor, project_id)
        return token, binding

    def access(
        self,
        actor: ActorContext,
        project_id: UUID | None = None,
        *,
        write: bool = False,
        account: str = "",
    ) -> tuple[str, str]:
        binding = self.binding(actor, self.project_id)
        connection = self.connected.connection(actor, account=binding.google_email)
        if write and (
            "jobs:write" not in actor.scopes or DRIVE_WRITE_SCOPE not in connection.scopes
        ):
            raise AuthorizationError("This Google account does not allow project file editing")
        self.connected.require_google_scope(connection, "drive_read")
        return self.connected.access_token(actor, connection), connection.email

    def assert_binding(self, actor: ActorContext, binding: ProjectDrive) -> None:
        self.project(self.current_actor(actor), binding.project_id)
        current = self.storage.location(actor, binding.project_id, self.location.id)
        if current != self.location:
            raise AuthorizationError("Project storage changed during this operation")
        live = self.binding(actor, binding.project_id)
        if (live.folder_id, live.google_email) != (binding.folder_id, binding.google_email):
            raise AuthorizationError("Project Drive folder changed during this operation")


class ProjectStorageService:
    def __init__(self, work: ProjectWorkService, connected: ConnectedService) -> None:
        self.work, self.connected = work, connected
        self.workspace = ProjectWorkspaceService(work)

    def _state(
        self, actor: ActorContext, project_id: UUID
    ) -> tuple[int, tuple[ProjectStorageLocation, ...]]:
        self.workspace.authorize(actor, project_id)
        job = self.workspace._job(actor, project_id, "storage", "locations")
        if job:
            saved = job.result or job.input["initial_state"]
            return job.version, tuple(
                ProjectStorageLocation.model_validate(row) for row in saved["locations"]
            )
        binding = self.connected.store.project_drive(actor.workspace_id, actor.actor_id, project_id)
        if binding and binding.enabled and binding.folder_id:
            return 0, (
                ProjectStorageLocation(
                    id="linked-drive",
                    name="Google Drive",
                    provider="google_drive",
                    linked_drive=True,
                    writable=True,
                ),
            )
        return 0, (
            ProjectStorageLocation(
                id="local", name="Project files", provider="local", writable=True
            ),
        )

    def resolved(
        self, actor: ActorContext, project_id: UUID, location: ProjectStorageLocation
    ) -> ProjectStorageLocation:
        if not location.linked_drive:
            return location
        binding = self.connected.store.project_drive(actor.workspace_id, actor.actor_id, project_id)
        if not binding or not binding.enabled or not binding.folder_id or not binding.google_email:
            raise ValidationError("Choose the project's Drive folder in Files")
        return location.model_copy(
            update={"folder_id": binding.folder_id, "account": binding.google_email}
        )

    def location(
        self, actor: ActorContext, project_id: UUID, identifier: str
    ) -> ProjectStorageLocation:
        location = next(
            (item for item in self._state(actor, project_id)[1] if item.id == identifier), None
        )
        if location is None:
            raise AuthorizationError("This file location is not selected for the project")
        return location

    def _record(
        self, actor: ActorContext, location: ProjectStorageLocation
    ) -> IntegrationConnection:
        record = next(
            (
                row
                for row in self.connected.store.integration_connections(
                    actor.workspace_id, actor.actor_id
                )
                if row.id == location.connection_id and row.provider == location.provider
            ),
            None,
        )
        if record is None:
            raise ValidationError("Reconnect this storage account in Connections")
        if record.settings.get("connection_test", {}).get("status") == "failed":
            raise ValidationError("Test or reconnect this account in Connections")
        return record

    def _ready(
        self, actor: ActorContext, project_id: UUID, location: ProjectStorageLocation
    ) -> bool:
        try:
            location = self.resolved(actor, project_id, location)
            if location.provider == "local":
                self.connected.local_files.authorize(actor)
                parts(location.path)
            elif location.provider == "google_drive":
                connection = self.connected.connection(actor, account=location.account)
                if not location.folder_id or not {DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE}.intersection(
                    connection.scopes
                ):
                    return False
            else:
                self._record(actor, location)
            return True
        except DomainError:
            return False

    def writable(self, actor: ActorContext, location: ProjectStorageLocation) -> bool:
        if not location.writable or "jobs:write" not in actor.scopes:
            return False
        if location.provider == "google_drive":
            try:
                return (
                    DRIVE_WRITE_SCOPE
                    in self.connected.connection(actor, account=location.account).scopes
                )
            except DomainError:
                return False
        if location.provider in {"box", "onedrive"}:
            return False
        return location.provider == "local" or bool(
            self._record(actor, location).settings.get("storage_write_enabled")
        )

    def snapshot(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        version, locations = self._state(actor, project_id)
        rows = []
        for location in locations:
            ready = self._ready(actor, project_id, location)
            resolved = self.resolved(actor, project_id, location) if ready else location
            rows.append(
                {
                    **resolved.model_dump(mode="json"),
                    "state": "ready" if ready else "unavailable",
                    "can_write": ready and self.writable(actor, resolved),
                }
            )
        return {"version": version, "locations": rows}

    def save(
        self, actor: ActorContext, project_id: UUID, body: SaveProjectStorage
    ) -> dict[str, Any]:
        self.workspace.authorize(actor, project_id, write=True)
        if actor.channel != Channel.API:
            raise AuthorizationError("Choose storage locations through the authenticated UI")
        version, previous = self._state(actor, project_id)
        if version != body.expected_version:
            raise InvalidTransitionError("Project storage changed; reload before saving")
        validated = []
        for location in body.locations:
            if location in previous:
                validated.append(location)
                continue
            resolved = self.resolved(actor, project_id, location)
            if not self._ready(actor, project_id, location):
                raise ValidationError("Connect the selected storage account first")
            if location.provider == "google_drive":
                assert resolved.folder_id is not None
                connection = self.connected.connection(actor, account=resolved.account)
                metadata = self.connected.projects.api.metadata(
                    self.connected.access_token(actor, connection), resolved.folder_id
                )
                if metadata.get("mimeType") != FOLDER:
                    raise ValidationError("Select a Google Drive folder")
                if not location.linked_drive:
                    location = location.model_copy(
                        update={"account": connection.email, "folder_id": metadata["id"]}
                    )
            elif location.provider == "local":
                self.connected.local_files.listing(
                    actor, LocalList(root="project:" + str(project_id), path=location.path)
                )
            else:
                relative_path(location.path, empty=True)
                if location.path and location.provider not in {"dropbox", "webdav"}:
                    raise ValidationError("Choose this provider's folder in Connections")
                self._cloud(actor, project_id, location, "list", {}, False)
            validated.append(location)
        with self.connected.store.transaction(actor.workspace_id):
            self.workspace.authorize(actor, project_id, write=True)
            current = self.workspace._job(actor, project_id, "storage", "locations")
            self.workspace._save(
                actor,
                project_id,
                "storage",
                "locations",
                current,
                body.expected_version,
                {"locations": [location.model_dump(mode="json") for location in validated]},
            )
        return self.snapshot(actor, project_id)

    def tool_filter(
        self, actor: ActorContext, project_id: UUID
    ) -> Callable[[ToolDefinition], bool]:
        ready = [
            row for row in self.snapshot(actor, project_id)["locations"] if row["state"] == "ready"
        ]
        return lambda tool: self.permits(tool, ready)

    @staticmethod
    def permits(tool: ToolDefinition, ready: list[dict[str, Any]]) -> bool:
        # Account-wide storage tools are replaced with the project-rooted operations.
        if tool.transport in STORAGE_TRANSPORTS or tool.id.startswith("native.drive_"):
            return False
        if tool.transport == "project_storage":
            return tool.id == "project.storage_locations" or (
                bool(ready)
                and (tool.id != "project.storage_write" or any(row["can_write"] for row in ready))
            )
        if tool.id.startswith("native.local_"):
            return False
        if tool.id == "workspace.import_local":
            return any(row["provider"] == "local" for row in ready)
        if tool.id.startswith(("native.project_file", "native.project_sheet")):
            return any(
                row["linked_drive"] and (not tool.side_effect or row["can_write"]) for row in ready
            )
        return True

    def agent_locations(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        rows = []
        for row in self.snapshot(actor, project_id)["locations"]:
            if row["state"] != "ready":
                continue
            location = self.location(actor, project_id, row["id"])
            if row["provider"] in STORAGE_TRANSPORTS:
                operations = [
                    {
                        "operation": tool.settings["operation"],
                        "write": tool.side_effect,
                        "arguments_schema": tool.input_schema,
                    }
                    for tool in self.connected.integrations._storage_tools(
                        self._record(actor, location)
                    )
                    if not tool.side_effect or row["can_write"]
                ]
                for operation in operations:
                    if operation["operation"] == "upload":
                        operation["operation"] = "write"
            elif row["provider"] == "google_drive":
                operations = [
                    {
                        "operation": operation,
                        "write": write,
                        "arguments_schema": model.model_json_schema(),
                    }
                    for operation, model, write in (
                        ("list", ProjectFiles, False),
                        ("read", ProjectFile, False),
                        ("sheet_read", ProjectSheetRead, False),
                        ("create", ProjectFileCreate, True),
                        ("edit", ProjectFileEdit, True),
                        ("rename", ProjectFileRename, True),
                        ("sheet_write", ProjectSheetWrite, True),
                    )
                    if not write or row["can_write"]
                ]
                for operation in operations:
                    schema = operation["arguments_schema"]
                    schema["properties"].pop("project_id", None)
                    schema["required"] = [
                        key for key in schema.get("required", []) if key != "project_id"
                    ]
            else:
                from simon.services.local_tool_schema import MODELS

                operations = [
                    {
                        "operation": operation,
                        "write": write,
                        "arguments_schema": MODELS[name].model_json_schema(),
                    }
                    for operation, name, write in (
                        ("list", "local_files_list", False),
                        ("read", "local_file_read", False),
                        ("write", "local_file_write", True),
                        ("edit", "local_file_edit", True),
                        ("folder_create", "local_folder_create", True),
                    )
                    if not write or row["can_write"]
                ]
                for operation in operations:
                    schema = operation["arguments_schema"]
                    schema["properties"].pop("root", None)
                    schema["required"] = [
                        key for key in schema.get("required", []) if key != "root"
                    ]
            rows.append({**row, "operations": operations})
        return {"locations": rows}

    def primary_selected(self, actor: ActorContext, project_id: UUID) -> bool:
        return any(location.linked_drive for location in self._state(actor, project_id)[1])

    def options(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        self.workspace.authorize(actor, project_id)
        return {
            "connections": [
                row
                for row in self.connected.integrations.list(actor)
                if row["provider"] in STORAGE_TRANSPORTS
            ],
            "google_accounts": [
                row.email
                for row in self.connected.store.google_connections(
                    actor.workspace_id, actor.actor_id
                )
            ],
        }

    def _cloud(
        self,
        actor: ActorContext,
        project_id: UUID,
        location: ProjectStorageLocation,
        operation: str,
        arguments: dict[str, Any],
        write: bool,
    ) -> dict[str, Any]:
        record = self._record(actor, location)
        settings = {**record.settings}
        subpath = relative_path(location.path, empty=True)
        if location.provider == "dropbox" and subpath:
            settings["root_path"] = settings["root_path"].rstrip("/") + "/" + subpath
        if location.provider == "webdav" and subpath:
            settings["endpoint"] = (
                settings["endpoint"].rstrip("/")
                + "/"
                + "/".join(quote(part, safe="") for part in subpath.split("/"))
            )
        record = record.model_copy(update={"settings": settings})
        operation = "upload" if write and location.provider == "dropbox" else operation
        definition = next(
            (
                tool
                for tool in self.connected.integrations._storage_tools(record)
                if tool.id == location.provider + "." + operation
            ),
            None,
        )
        if definition is None:
            raise ValidationError("This operation is not supported by the selected location")
        if definition.side_effect != write:
            raise AuthorizationError("Storage operation action policy changed")
        context = ToolExecutionContext(
            actor_id=actor.actor_id,
            workspace_id=actor.workspace_id,
            run_id=project_id,
            agent_id="project-storage",
            allowed_tool_ids=frozenset({definition.id}),
            scopes=actor.scopes,
            authorized_action="write" if write else "read",
        )
        transport: Any = {
            "box": BoxTransport,
            "dropbox": DropboxTransport,
            "onedrive": OneDriveTransport,
            "webdav": WebDAVTransport,
        }[location.provider](
            environ=self.connected.integrations.credentials,
            transport=self.connected.integrations.clickup.http.transport,
        )
        return cast(dict[str, Any], transport(definition, arguments, context))

    def execute(
        self,
        actor: ActorContext,
        project_id: UUID,
        location_id: str,
        operation: str,
        arguments: dict[str, Any],
        *,
        write: bool = False,
        key: str = "",
        revalidate: Callable[[], ActorContext] | None = None,
    ) -> dict[str, Any]:
        self.workspace.authorize(actor, project_id, write=write)
        original = self.location(actor, project_id, location_id)
        reads = {"list", "search", "metadata", "read", "sheet_read"}
        if write == (operation in reads):
            raise AuthorizationError("The storage operation requires a different action policy")
        location = self.resolved(actor, project_id, original)
        if write:
            key = (
                key
                + ":"
                + digest({"project": str(project_id), "location": location.model_dump(mode="json")})
            )
        if not self._ready(actor, project_id, original) or (
            write and not self.writable(actor, location)
        ):
            raise AuthorizationError(
                "This project location is unavailable for the requested operation"
            )

        def check() -> ActorContext:
            current = revalidate() if revalidate else actor
            if (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id):
                raise AuthorizationError("Project file access changed")
            self.workspace.authorize(current, project_id, write=write)
            if self.location(current, project_id, location_id) != original:
                raise AuthorizationError("Project storage changed during this operation")
            return current

        check()
        if location.provider == "google_drive":
            projects = _LocationDrive(self, actor, project_id, original)
            models: dict[str, tuple[type[StrictModel], str]] = {
                "list": (ProjectFiles, "files"),
                "read": (ProjectFile, "read"),
                "create": (ProjectFileCreate, "create_file"),
                "edit": (ProjectFileEdit, "edit"),
                "rename": (ProjectFileRename, "rename"),
                "sheet_read": (ProjectSheetRead, "sheet_read"),
                "sheet_write": (ProjectSheetWrite, "sheet_write"),
            }
            if operation not in models:
                raise ValidationError("Choose an operation supported by Google Drive")
            model, method = models[operation]
            request = model.model_validate({**arguments, "project_id": project_id})
            result = (
                getattr(projects, method)(actor, request, key, check)
                if write
                else getattr(projects, method)(actor, request)
            )
        elif location.provider == "local":
            path = "/".join((*parts(location.path), *parts(arguments.get("path", ""))))
            values = {**arguments, "root": "project:" + str(project_id), "path": path}
            if write:
                name = {
                    "write": "local_file_write",
                    "edit": "local_file_edit",
                    "folder_create": "local_folder_create",
                }.get(operation)
                if name is None:
                    raise ValidationError("Choose an operation supported by local files")
                result = self.connected.local_files.run(actor, name, values, key, check)
            else:
                if operation == "list":
                    result = self.connected.local_files.listing(
                        actor, LocalList.model_validate(values)
                    )
                elif operation == "read":
                    result = self.connected.local_files.read(
                        actor, LocalRead.model_validate(values)
                    )
                else:
                    raise ValidationError("Choose list or read for local files")
        else:
            result = self._cloud(actor, project_id, location, operation, arguments, write)
        check()
        return {**result, "project_id": str(project_id), "location_id": location_id}
