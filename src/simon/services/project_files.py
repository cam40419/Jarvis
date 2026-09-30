"""Drive is the live project file store; local task artifacts remain immutable originals."""

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from simon.adapters.google import DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE, ConnectedError
from simon.adapters.project_drive import DOC, FOLDER, SHEET, DriveError, ProjectDriveAPI
from simon.domain.context import CreateMemory, ExplicitMemory
from simon.domain.errors import AuthorizationError, DomainError, NotFoundError, ValidationError
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.project_files import (
    DriveBrowse,
    ProjectBind,
    ProjectCreate,
    ProjectDrive,
    ProjectFile,
    ProjectFileCreate,
    ProjectFileEdit,
    ProjectFileOperation,
    ProjectFileRename,
    ProjectFiles,
    ProjectSheetRead,
    ProjectSheetWrite,
    ProjectTrash,
    ProjectUnlink,
)
from simon.services.canonical import digest
from simon.services.identity import IDENTITY_LOCK, ROLE_SCOPES

if TYPE_CHECKING:
    from simon.services.connected import ConnectedService


class ProjectFileService:
    def __init__(self, connected: "ConnectedService") -> None:
        self.connected, self.store = connected, connected.store
        self.api = ProjectDriveAPI()

    def execute(
        self,
        actor: ActorContext,
        run_id: UUID,
        name: str,
        arguments: str,
        revalidate: Callable[[], ActorContext],
    ) -> str:
        from simon.services.project_tool_schema import READS

        def check() -> ActorContext:
            current = revalidate()
            if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Project access changed.")
            attempt = self.store.attempt(run_id)
            if (
                not attempt
                or attempt.status != "pending"
                or attempt.expires_at <= utc_now()
                or attempt.run.actor_id != actor.actor_id
                or attempt.household_id != actor.household_id
            ):
                raise AuthorizationError("Active request not found.")
            thread = self.connected.conversations.get(current, attempt.run.thread_id)
            if thread.visibility != "personal":
                raise AuthorizationError("Use a private conversation for project files.")
            if name not in READS and attempt.run.parent_run_id:
                raise AuthorizationError("A fresh request is required to edit project files.")
            if name not in attempt.run.capability_manifest:
                raise AuthorizationError("Project tool was not enabled for this request.")
            if name not in self.connected.available(current):
                raise AuthorizationError("Project tool access changed.")
            return current

        current = check()
        values = json.loads(arguments)
        key = f"{run_id}:{name}:{digest(values)}"
        if name == "project_list":
            if values:
                raise ValidationError("No arguments expected.")
            result: Any = self.list(current)
        elif name == "project_create":
            result = self.create(
                current, ProjectCreate.model_validate({**values, "idempotency_key": key})
            )
        elif name == "project_unlink_drive":
            result = self.unlink(current, ProjectUnlink.model_validate(values))
        elif name == "project_drive_trash":
            result = self.trash(current, ProjectTrash.model_validate(values), key, check)
        elif name == "project_link_drive":
            result = self.bind(current, ProjectBind.model_validate(values))
        elif name == "project_sync":
            from simon.domain.project_files import ProjectSelect

            result = self.sync(current, ProjectSelect.model_validate(values).project_id, force=True)
        elif name == "project_files_list":
            result = self.files(current, ProjectFiles.model_validate(values))
        elif name == "project_file_read":
            result = self.read(current, ProjectFile.model_validate(values))
        elif name == "project_file_create":
            result = self.create_file(current, ProjectFileCreate.model_validate(values), key, check)
        elif name == "project_file_edit":
            result = self.edit(current, ProjectFileEdit.model_validate(values), key, check)
        elif name == "project_sheet_read":
            result = self.sheet_read(current, ProjectSheetRead.model_validate(values))
        elif name == "project_sheet_write":
            result = self.sheet_write(current, ProjectSheetWrite.model_validate(values), key, check)
        elif name == "project_file_rename":
            result = self.rename(current, ProjectFileRename.model_validate(values), key, check)
        else:
            raise ValidationError("Unknown project tool.")
        check()
        return json.dumps(result, ensure_ascii=False)

    def project(self, actor: ActorContext, identifier: UUID) -> ExplicitMemory:
        if "jobs:read" not in actor.scopes or "memories:read" not in actor.scopes:
            raise AuthorizationError("Project access is unavailable.")
        project = self.store.explicit_memory(actor.household_id, identifier)
        if (
            not project
            or not project.accepted
            or project.category != "project"
            or (project.scope == "personal" and project.created_by != actor.actor_id)
        ):
            raise NotFoundError("Project not found.")
        return project

    def binding(self, actor: ActorContext, project_id: UUID) -> ProjectDrive:
        project = self.project(actor, project_id)
        with self.store.transaction(actor.household_id):
            existing = self.store.project_drive(actor.household_id, actor.actor_id, project_id)
            if existing:
                return existing
            # Personal context edits supersede memory records; keep the same live folder.
            prior_id = project.supersedes
            for _ in range(100):
                if not prior_id:
                    break
                prior = self.store.explicit_memory(actor.household_id, prior_id)
                if (
                    not prior
                    or prior.created_by != project.created_by
                    or prior.category != "project"
                ):
                    break
                inherited = self.store.project_drive(actor.household_id, actor.actor_id, prior_id)
                if inherited and (inherited.folder_id or not inherited.enabled):
                    binding = inherited.model_copy(
                        update={
                            "project_id": project_id,
                            "version": 1,
                            "lease_until": utc_now(),
                            "next_sync_at": utc_now(),
                        }
                    )
                    self.store.save_project_drive(binding)
                    return binding
                prior_id = prior.supersedes
            binding = ProjectDrive(
                project_id=project_id,
                household_id=actor.household_id,
                actor_id=actor.actor_id,
            )
            self.store.save_project_drive(binding)
            return binding

    def access(
        self,
        actor: ActorContext,
        project_id: UUID | None = None,
        *,
        write: bool = False,
        account: str = "",
    ) -> tuple[str, str]:
        if write and "jobs:write" not in actor.scopes:
            raise AuthorizationError("Project editing permission is unavailable.")
        if project_id and not account:
            account = self.binding(actor, project_id).google_email
        connection = self.connected.connection(actor, account=account)
        if DRIVE_WRITE_SCOPE not in connection.scopes:
            raise ConnectedError(
                "Reconnect Google in Connections and allow Drive editing for projects."
            )
        return self.connected.access_token(actor, connection), connection.email

    def view(self, actor: ActorContext, binding: ProjectDrive) -> dict[str, Any]:
        connections = self.store.google_connections(actor.household_id, actor.actor_id)
        connection = (
            next((c for c in connections if c.email == binding.google_email), None)
            if binding.google_email
            else (connections[0] if connections else None)
        )
        ready = bool(connection and DRIVE_WRITE_SCOPE in connection.scopes)
        result = binding.model_dump(mode="json", exclude={"lease_until", "next_sync_at"})
        if not binding.enabled:
            result.update(status="unlinked", error=None)
        elif not ready:
            result.update(
                status="needs_permission", error="Reconnect Google with Drive editing access."
            )
        elif binding.google_email and connection and binding.google_email != connection.email:
            result.update(
                status="error", error="Google account changed. Link this project's folder again."
            )
        result["url"] = (
            f"https://drive.google.com/drive/folders/{binding.folder_id}"
            if binding.folder_id
            else None
        )
        return result

    def list(self, actor: ActorContext) -> list[dict[str, Any]]:
        if not {"jobs:read", "memories:read"} <= actor.scopes:
            raise AuthorizationError("Project access is unavailable.")
        return [
            {
                **project.model_dump(mode="json"),
                "drive": self.view(actor, self.binding(actor, project.id)),
            }
            for project in self.store.explicit_memories(actor.household_id, 0, 500, actor.actor_id)
            if project.category == "project"
        ]

    def create(self, actor: ActorContext, request: ProjectCreate) -> dict[str, Any]:
        if request.account:
            self.access(actor, write=True, account=request.account)
        project = self.connected.memories.create(
            actor,
            CreateMemory(
                subject=request.name,
                content=request.description,
                scope="personal",
                category="project",
                idempotency_key=request.idempotency_key,
            ),
        )
        binding = self.binding(actor, project.id)
        if request.account and not binding.folder_id and not binding.google_email:
            binding = binding.model_copy(
                update={
                    "google_email": self.connected.connection(actor, account=request.account).email
                }
            )
            self.store.save_project_drive(binding)
        try:
            binding = self.ensure(actor, project.id)
        except DomainError as error:
            binding = binding.model_copy(update={"status": "needs_permission", "error": str(error)})
            self.store.save_project_drive(binding)
        return {**project.model_dump(mode="json"), "drive": self.view(actor, binding)}

    def operate(
        self,
        actor: ActorContext,
        project_id: UUID,
        key: str,
        kind: str,
        data: dict[str, Any],
        perform: Callable[[str, ProjectFileOperation], dict[str, Any]],
        revalidate: Callable[[], ActorContext],
        *,
        before: dict[str, Any] | None = None,
        generated_id: bool = False,
        retry_create: bool = False,
        account: str = "",
    ) -> ProjectFileOperation:
        self.project(actor, project_id)
        token, email = self.access(actor, project_id, write=True, account=account)
        identifier = uuid5(
            NAMESPACE_URL, f"project-file:{actor.household_id}:{actor.actor_id}:{key}"
        )
        request_digest = digest({"project_id": str(project_id), "kind": kind, "data": data})
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            current = revalidate()
            if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Project access changed.")
            self.project(current, project_id)
            existing = self.store.project_file_operation(identifier)
            if existing:
                if existing.request_digest != request_digest or existing.google_email != email:
                    raise ValidationError(
                        "This operation key belongs to a different request/account."
                    )
                recoverable = (
                    retry_create
                    and generated_id
                    and existing.status != "succeeded"
                    and (
                        existing.status != "executing"
                        or existing.created_at + timedelta(minutes=5) < utc_now()
                    )
                )
                if not recoverable:
                    return existing
            operation = existing or ProjectFileOperation(
                id=identifier,
                household_id=actor.household_id,
                actor_id=actor.actor_id,
                project_id=project_id,
                google_email=email,
                request_digest=request_digest,
                kind=kind,
                before=before or {},
            )
            operation = operation.model_copy(
                update={"status": "executing", "created_at": utc_now()}
            )
            self.store.save_project_file_operation(operation)
        try:
            if generated_id and not operation.file_id:
                operation = operation.model_copy(update={"file_id": self.api.generate_id(token)})
                self.store.save_project_file_operation(operation)
            current = revalidate()
            if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Project access changed.")
            self.project(current, project_id)
            token, current_email = self.access(current, project_id, write=True, account=account)
            if current_email != email:
                raise AuthorizationError("Google account changed during this request.")
            result = perform(token, operation)
            operation = operation.model_copy(
                update={
                    "status": "succeeded",
                    "result": result,
                    "file_id": result.get("id", operation.file_id),
                    "error": None,
                }
            )
        except Exception as error:
            operation = operation.model_copy(
                update={
                    "status": "unknown"
                    if not isinstance(error, DomainError)
                    or (isinstance(error, ConnectedError) and error.unknown)
                    else "failed",
                    "error": str(error)
                    if isinstance(error, DomainError)
                    else "File operation could not be completed. Inspect Drive before retrying.",
                }
            )
        with self.store.transaction(actor.household_id):
            self.store.save_project_file_operation(operation)
            self.connected.audit.record(
                event_type="project.file." + operation.status,
                actor=actor,
                resource_type="project",
                resource_id=str(project_id),
                payload={"operation_id": str(operation.id), "kind": kind},
            )
        return operation

    @staticmethod
    def receipt(operation: ProjectFileOperation) -> dict[str, Any]:
        return operation.model_dump(mode="json", exclude={"before", "request_digest"})

    def ensure(self, actor: ActorContext, project_id: UUID) -> ProjectDrive:
        project = self.project(actor, project_id)
        binding = self.binding(actor, project_id)
        if not binding.enabled:
            raise ValidationError(
                "This project has no linked Drive folder. Link one to resume Drive access."
            )
        token, email = self.access(actor, project_id, write=True)
        if binding.google_email and binding.google_email != email:
            raise AuthorizationError("Google account changed. Link the project folder again.")
        if not binding.google_email:
            with self.store.transaction(actor.household_id):
                binding = self.binding(actor, project_id)
                if not binding.google_email:
                    binding = binding.model_copy(update={"google_email": email})
                    self.store.save_project_drive(binding)
                elif binding.google_email != email:
                    raise AuthorizationError("Project account changed. Retry the request.")
        if binding.folder_id:
            folder = self.api.metadata(token, binding.folder_id)
            if folder["mimeType"] != FOLDER:
                raise ValidationError("The project's Drive location is not a folder.")
            return binding

        def create(access: str, operation: ProjectFileOperation) -> dict[str, Any]:
            assert operation.file_id
            try:
                return self.api.metadata(access, operation.file_id)
            except DriveError as error:
                if error.status != 404:
                    raise
            return self.api.create(
                access,
                "Simon - " + project.subject,
                None,
                None,
                FOLDER,
                operation.file_id,
                str(operation.id),
            )

        operation = self.operate(
            actor,
            project_id,
            f"folder:{project_id}:{email}",
            "create_folder",
            {},
            create,
            lambda: self.current_actor(actor),
            generated_id=True,
            retry_create=True,
        )
        if operation.status != "succeeded":
            raise ConnectedError(operation.error or "Project folder creation is still in progress.")
        with self.store.transaction(actor.household_id):
            current = self.binding(actor, project_id)
            if not current.enabled or current.version != binding.version:
                raise AuthorizationError("Project folder changed during creation.")
            if current.folder_id:
                return current
            binding = current.model_copy(
                update={
                    "folder_id": operation.file_id,
                    "google_email": email,
                    "status": "ready",
                    "error": None,
                }
            )
            self.store.save_project_drive(binding)
        return binding

    def current_actor(self, actor: ActorContext) -> ActorContext:
        member = self.connected.identity.membership(actor.actor_id, actor.household_id)
        return actor.model_copy(update={"scopes": ROLE_SCOPES[member.role]})

    def bind(self, actor: ActorContext, request: ProjectBind) -> dict[str, Any]:
        self.project(actor, request.project_id)
        token, email = self.access(actor, write=True, account=request.account)
        folder = self.api.metadata(token, request.folder_id)
        if folder["mimeType"] != FOLDER or not (
            folder.get("capabilities", {}).get("canAddChildren")
            or folder.get("capabilities", {}).get("canEdit")
        ):
            raise ValidationError("Choose a Drive folder you can edit.")
        self.project(self.current_actor(actor), request.project_id)
        if self.access(actor, write=True, account=email)[1] != email:
            raise AuthorizationError("Google account changed.")
        with self.store.transaction(actor.household_id):
            old = self.store.project_drive(actor.household_id, actor.actor_id, request.project_id)
            if (old.version if old else 0) != request.expected_version:
                raise ValidationError(
                    "Project folder changed. Refresh the project before relinking."
                )
            binding = ProjectDrive(
                project_id=request.project_id,
                household_id=actor.household_id,
                actor_id=actor.actor_id,
                google_email=email,
                folder_id=folder["id"],
                status="ready",
                version=request.expected_version + 1,
            )
            self.store.save_project_drive(binding)
        return self.view(actor, binding)

    def unlink(self, actor: ActorContext, request: ProjectUnlink) -> dict[str, Any]:
        self.connected.conversations.authorize(actor, "jobs:write")
        self.project(actor, request.project_id)
        with self.store.transaction(actor.household_id):
            binding = self.binding(actor, request.project_id)
            if not binding.enabled and binding.version == request.expected_version + 1:
                return self.view(actor, binding)
            if binding.version != request.expected_version:
                raise ValidationError("Project folder changed. Refresh before unlinking.")
            binding = binding.model_copy(
                update={
                    "folder_id": None,
                    "enabled": False,
                    "status": "unlinked",
                    "error": None,
                    "version": binding.version + 1,
                    "lease_until": utc_now(),
                }
            )
            self.store.save_project_drive(binding)
            self.connected.audit.record(
                event_type="project.drive.unlinked",
                actor=actor,
                resource_type="project",
                resource_id=str(request.project_id),
                payload={},
            )
            return self.view(actor, binding)

    def browse(
        self,
        actor: ActorContext,
        request: DriveBrowse,
        revalidate: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        self.connected.conversations.authorize(actor, "threads:read")
        connection = self.connected.connection(actor, account=request.account)
        if not {DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE}.intersection(connection.scopes):
            raise ConnectedError("Reconnect this Google account with Drive access.")
        token = self.connected.access_token(actor, connection)
        folder = self.api.metadata(token, request.folder_id)
        if folder["mimeType"] != FOLDER:
            raise ValidationError("Select a Drive folder to browse.")
        result = self.api.list_files(
            token,
            folder["id"],
            request.query,
            request.page_token,
            folders_only=request.folders_only,
            search_all=request.search_all,
        )
        current = revalidate()
        if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
            raise AuthorizationError("Drive access changed.")
        self.connected.conversations.authorize(current, "threads:read")
        latest = self.connected.connection(current, connection.id)
        if not {DRIVE_READ_SCOPE, DRIVE_WRITE_SCOPE}.intersection(latest.scopes):
            raise AuthorizationError("Drive access changed.")
        return {"account_email": connection.email, "folder": folder, **result}

    def trash(
        self,
        actor: ActorContext,
        request: ProjectTrash,
        key: str,
        revalidate: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        saved = self.replay(actor, request, key, "trash", revalidate)
        if saved:
            return saved
        token, email = self.access(actor, request.project_id, write=True, account=request.account)
        current = self.api.metadata(token, request.file_id)
        root = self.api.metadata(token, "root")
        if current["id"] == root["id"]:
            raise ValidationError("My Drive itself cannot be deleted.")
        if str(current.get("version", "")) != request.revision:
            raise ValidationError("Drive item changed. Browse it again before deleting.")
        if not current.get("capabilities", {}).get("canTrash"):
            raise ValidationError("Google does not allow this item to be moved to trash.")

        def protect_linked_folder(access: str) -> None:
            binding = self.binding(actor, request.project_id)
            if not binding.enabled or not binding.folder_id or binding.google_email != email:
                return
            identifier = binding.folder_id
            visited: set[str] = set()
            for _ in range(100):
                if identifier in visited:
                    break
                visited.add(identifier)
                if identifier == current["id"]:
                    raise ValidationError(
                        "Relink or unlink the active project folder before trashing it or a "
                        "parent folder."
                    )
                metadata = self.api.metadata(access, identifier)
                if not metadata.get("parents"):
                    return
                identifier = metadata["parents"][0]
            raise ValidationError("Could not verify the active project's folder ancestry.")

        protect_linked_folder(token)

        def write(access: str, operation: ProjectFileOperation) -> dict[str, Any]:
            protect_linked_folder(access)
            latest = self.api.metadata(access, request.file_id)
            if str(latest.get("version", "")) != request.revision:
                raise ValidationError("Drive item changed before deletion. Browse it again.")
            return {
                **self.api.trash(access, latest),
                "account_email": email,
                "permanently_deleted": False,
            }

        return self.receipt(
            self.operate(
                actor,
                request.project_id,
                key,
                "trash",
                request.model_dump(mode="json"),
                write,
                revalidate,
                before=current,
                account=request.account,
            )
        )

    def within(
        self,
        token: str,
        binding: ProjectDrive,
        identifier: str,
    ) -> dict[str, Any]:
        original = self.api.metadata(token, identifier)
        current = original
        visited = set()
        for _ in range(32):
            if current["id"] == binding.folder_id:
                return original
            if current["id"] in visited or not current.get("parents"):
                break
            visited.add(current["id"])
            current = self.api.metadata(token, current["parents"][0])
        raise AuthorizationError("The file is outside this project's linked Drive folder.")

    def context(self, actor: ActorContext, project_id: UUID) -> tuple[str, ProjectDrive]:
        binding = self.ensure(actor, project_id)
        token, email = self.access(actor, project_id)
        if email != binding.google_email:
            raise AuthorizationError("Google account changed.")
        return token, binding

    def files(self, actor: ActorContext, request: ProjectFiles) -> dict[str, Any]:
        token, binding = self.context(actor, request.project_id)
        folder_id = request.folder_id or binding.folder_id
        assert folder_id
        folder = self.within(token, binding, folder_id)
        if folder["mimeType"] != FOLDER:
            raise ValidationError("Select a folder to list files.")
        result = {
            "project_id": str(request.project_id),
            "folder": folder,
            **self.api.list_files(token, folder_id, request.query, request.page_token),
        }
        self.assert_binding(actor, binding)
        return result

    def read(self, actor: ActorContext, request: ProjectFile) -> dict[str, Any]:
        token, binding = self.context(actor, request.project_id)
        result = self.api.read(token, self.within(token, binding, request.file_id))
        self.assert_binding(actor, binding)
        return result

    def create_file(
        self,
        actor: ActorContext,
        request: ProjectFileCreate,
        key: str,
        revalidate: Callable[[], ActorContext],
        *,
        raw: bytes | None = None,
        media_type: str | None = None,
        retry_create: bool = False,
    ) -> dict[str, Any]:
        token, binding = self.context(actor, request.project_id)
        parent = request.folder_id or binding.folder_id
        assert parent
        if self.within(token, binding, parent)["mimeType"] != FOLDER:
            raise ValidationError("Select a folder for the new file.")
        mime = media_type or self.api.media(request.name, request.format)
        content = None if mime == FOLDER else raw if raw is not None else request.content.encode()
        data = {
            "name": request.name,
            "parent": parent,
            "mime": mime,
            "sha256": hashlib.sha256(content or b"").hexdigest(),
        }

        def create(access: str, operation: ProjectFileOperation) -> dict[str, Any]:
            self.assert_binding(actor, binding)
            self.within(access, binding, parent)
            if operation.file_id:
                try:
                    return self.api.metadata(access, operation.file_id)
                except DriveError as error:
                    if error.status != 404:
                        raise
            result = self.api.create(
                access, request.name, parent, content, mime, operation.file_id, str(operation.id)
            )
            return {**result, "url": f"https://drive.google.com/file/d/{result['id']}/view"}

        return self.receipt(
            self.operate(
                actor,
                request.project_id,
                key,
                "create",
                data,
                create,
                revalidate,
                generated_id=mime not in {DOC, SHEET},
                retry_create=retry_create,
            )
        )

    def assert_binding(self, actor: ActorContext, binding: ProjectDrive) -> None:
        self.project(self.current_actor(actor), binding.project_id)
        if self.access(actor, binding.project_id)[1] != binding.google_email:
            raise AuthorizationError("Google account changed.")
        current = self.binding(actor, binding.project_id)
        if (current.version, current.folder_id, current.google_email) != (
            binding.version,
            binding.folder_id,
            binding.google_email,
        ):
            raise AuthorizationError("Project folder changed during this operation.")

    def replay(
        self,
        actor: ActorContext,
        request: Any,
        key: str,
        kind: str,
        revalidate: Callable[[], ActorContext],
    ) -> dict[str, Any] | None:
        current = revalidate()
        if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
            raise AuthorizationError("Project access changed.")
        self.project(current, request.project_id)
        _, email = self.access(
            current, request.project_id, write=True, account=getattr(request, "account", "")
        )
        identifier = uuid5(
            NAMESPACE_URL, f"project-file:{actor.household_id}:{actor.actor_id}:{key}"
        )
        operation = self.store.project_file_operation(identifier)
        if not operation:
            return None
        expected = digest(
            {
                "project_id": str(request.project_id),
                "kind": kind,
                "data": request.model_dump(mode="json"),
            }
        )
        if operation.request_digest != expected or operation.google_email != email:
            raise ValidationError("Operation key belongs to another request/account.")
        return self.receipt(operation)

    def edit(
        self,
        actor: ActorContext,
        request: ProjectFileEdit,
        key: str,
        revalidate: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        saved = self.replay(actor, request, key, "edit", revalidate)
        if saved:
            return saved
        token, binding = self.context(actor, request.project_id)
        current = self.api.read(token, self.within(token, binding, request.file_id))
        if current["revision"] != request.revision:
            raise ValidationError("File changed since it was read. Read again and apply the edit.")

        def write(access: str, operation: ProjectFileOperation) -> dict[str, Any]:
            self.assert_binding(actor, binding)
            latest = self.within(access, binding, request.file_id)
            if latest.get("version") != current.get("version"):
                raise ValidationError("File changed before saving. Read it again.")
            result = self.api.edit(
                access, current, request.old_text, request.new_text, request.tab_id
            )
            return {**result, "previous_revision": request.revision}

        return self.receipt(
            self.operate(
                actor,
                request.project_id,
                key,
                "edit",
                request.model_dump(mode="json"),
                write,
                revalidate,
                before=current,
            )
        )

    def sheet_read(self, actor: ActorContext, request: ProjectSheetRead) -> dict[str, Any]:
        token, binding = self.context(actor, request.project_id)
        if self.within(token, binding, request.file_id)["mimeType"] != SHEET:
            raise ValidationError("Select a Google Sheet.")
        result = self.api.sheet_read(token, request.file_id, request.range)
        self.assert_binding(actor, binding)
        return result

    def sheet_write(
        self,
        actor: ActorContext,
        request: ProjectSheetWrite,
        key: str,
        revalidate: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        saved = self.replay(actor, request, key, "sheet_write", revalidate)
        if saved:
            return saved
        current = self.sheet_read(actor, request)
        if current["revision"] != request.revision:
            raise ValidationError("Sheet cells changed. Read the range again before writing.")
        _, binding = self.context(actor, request.project_id)

        def write(access: str, operation: ProjectFileOperation) -> dict[str, Any]:
            self.assert_binding(actor, binding)
            self.within(access, binding, request.file_id)
            if (
                self.api.sheet_read(access, request.file_id, request.range)["revision"]
                != request.revision
            ):
                raise ValidationError("Sheet cells changed before saving. Read the range again.")
            result = self.api.sheet_write(access, request.file_id, request.range, request.values)
            return {
                "id": request.file_id,
                "url": f"https://docs.google.com/spreadsheets/d/{request.file_id}",
                **result,
            }

        return self.receipt(
            self.operate(
                actor,
                request.project_id,
                key,
                "sheet_write",
                request.model_dump(mode="json"),
                write,
                revalidate,
                before=current,
            )
        )

    def rename(
        self,
        actor: ActorContext,
        request: ProjectFileRename,
        key: str,
        revalidate: Callable[[], ActorContext],
    ) -> dict[str, Any]:
        saved = self.replay(actor, request, key, "rename", revalidate)
        if saved:
            return saved
        token, binding = self.context(actor, request.project_id)
        current = self.within(token, binding, request.file_id)
        if str(current.get("version", "")) != request.revision:
            raise ValidationError("File changed. List files again before renaming.")

        def write(access: str, operation: ProjectFileOperation) -> dict[str, Any]:
            self.assert_binding(actor, binding)
            latest = self.within(access, binding, request.file_id)
            if latest.get("version") != current.get("version"):
                raise ValidationError("File changed before renaming.")
            return self.api.rename(access, latest, request.name)

        return self.receipt(
            self.operate(
                actor,
                request.project_id,
                key,
                "rename",
                request.model_dump(mode="json"),
                write,
                revalidate,
                before=current,
            )
        )

    def sync(self, actor: ActorContext, project_id: UUID, *, force: bool = False) -> dict[str, Any]:
        binding = self.binding(actor, project_id)
        with self.store.transaction(actor.household_id):
            binding = self.binding(actor, project_id)
            if (
                not binding.enabled
                or binding.lease_until > utc_now()
                or (not force and binding.next_sync_at > utc_now())
            ):
                return self.view(actor, binding)
            self.store.save_project_drive(
                binding.model_copy(
                    update={
                        "lease_until": utc_now() + timedelta(minutes=10),
                    }
                )
            )
        error = None
        uploaded = 0
        try:
            binding = self.ensure(actor, project_id)
            # Existing task artifacts are immutable: upload once, then preserve Drive-side edits.
            uploaded = 0
            for offset in range(0, 10000, 100):
                artifacts = self.store.project_artifacts(
                    actor.household_id, actor.actor_id, project_id, offset, 100
                )
                for artifact in artifacts:
                    key = f"artifact:{artifact.id}:{binding.folder_id}:{binding.google_email}"
                    identifier = uuid5(
                        NAMESPACE_URL, f"project-file:{actor.household_id}:{actor.actor_id}:{key}"
                    )
                    receipt = self.store.project_file_operation(identifier)
                    if receipt and receipt.status == "succeeded":
                        continue
                    record = self.store.project_artifact(artifact.id)
                    if not record:
                        continue
                    result = self.create_file(
                        actor,
                        ProjectFileCreate(
                            project_id=project_id,
                            name=f"{str(artifact.task_id)[:8]}-{artifact.name}",
                        ),
                        key,
                        lambda: self.current_actor(actor),
                        raw=record[1],
                        media_type=artifact.media_type,
                        retry_create=True,
                    )
                    if result["status"] != "succeeded":
                        raise ConnectedError(result.get("error") or "File upload is still pending.")
                    uploaded += 1
                    if uploaded >= 5:
                        break
                if uploaded >= 5 or len(artifacts) < 100:
                    break
        except Exception as failure:
            error = (
                str(failure)
                if isinstance(failure, DomainError)
                else "Drive sync failed; retrying later."
            )
        finally:
            with self.store.transaction(actor.household_id):
                current = self.binding(actor, project_id)
                if current.version == binding.version:
                    current = current.model_copy(
                        update={
                            "status": "error" if error else "pending" if uploaded >= 5 else "ready",
                            "error": error,
                            "last_synced_at": current.last_synced_at if error else utc_now(),
                            "lease_until": utc_now(),
                            "next_sync_at": utc_now() + timedelta(seconds=60),
                        }
                    )
                    self.store.save_project_drive(current)
        return self.view(actor, current)

    def tick(self) -> None:
        if not self.connected.configured:
            return
        for household_id, actor_id in self.store.google_accounts():
            try:
                member = self.connected.identity.membership(actor_id, household_id)
                actor = ActorContext(
                    actor_id=actor_id,
                    household_id=household_id,
                    channel=Channel.WORKER,
                    scopes=ROLE_SCOPES[member.role],
                )
                if not {"jobs:write", "memories:read"} <= actor.scopes:
                    continue
                for project in self.list(actor):
                    try:
                        self.sync(actor, UUID(project["id"]))
                    except DomainError:
                        continue
            except DomainError:
                continue
