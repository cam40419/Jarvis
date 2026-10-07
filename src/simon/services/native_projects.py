"""Shared project authority with atomic board edits, current access and durable retries."""

from collections.abc import Callable
from typing import TypeVar
from uuid import UUID

from simon.domain.errors import AuthorizationError, InvalidTransitionError, NotFoundError
from simon.domain.identity import Membership
from simon.domain.models import ActorContext, utc_now
from simon.domain.native_projects import (
    CreateNativeProject,
    CreateNativeTask,
    NativeCommand,
    NativeModel,
    NativeProject,
    NativeProjectMember,
    NativeTask,
    PutNativeProjectMember,
    TaskAssignment,
    UpdateNativeProject,
    UpdateNativeTask,
    VersionedNativeCommand,
)
from simon.domain.ports import Store
from simon.services.audit import AuditService
from simon.services.canonical import digest
from simon.services.identity import IDENTITY_LOCK, ROLE_SCOPES

Result = TypeVar("Result", bound=NativeModel)


class NativeProjectService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.audit = AuditService(store)

    def _membership(self, workspace_id: UUID, actor_id: UUID) -> Membership | None:
        account = self.store.managed_account(actor_id)
        if account and account.disabled:
            return None
        return next(
            (m for m in self.store.memberships(actor_id) if m.workspace_id == workspace_id), None
        )

    def _workspace(self, actor: ActorContext, *, write: bool = False) -> Membership:
        membership = self._membership(actor.workspace_id, actor.actor_id)
        required = "jobs:write" if write else "jobs:read"
        if not write and membership is not None and membership.role == "guest":
            # Guest access is specific to an explicitly shared native project, not legacy jobs.
            required = "system:read"
        if (
            membership is None
            or required not in actor.scopes
            or required not in ROLE_SCOPES[membership.role]
        ):
            raise AuthorizationError("Current workspace access is required.")
        return membership

    def _project(
        self, actor: ActorContext, project_id: UUID, *, write: bool = False, owner: bool = False
    ) -> NativeProject:
        workspace = self._workspace(actor, write=write)
        project = self.store.native_project(actor.workspace_id, project_id)
        member = self.store.native_project_member(actor.workspace_id, project_id, actor.actor_id)
        if project is None or (workspace.role != "owner" and member is None):
            raise NotFoundError("Project not found.")
        if owner and workspace.role != "owner" and (member is None or member.role != "owner"):
            raise AuthorizationError("Project owner access is required.")
        return project

    @staticmethod
    def _page(offset: int, limit: int) -> None:
        if offset < 0 or offset > 1_000_000 or not 1 <= limit <= 100:
            from simon.domain.errors import ValidationError

            raise ValidationError("Invalid page bounds.")

    @staticmethod
    def _version(actual: int, expected: int) -> None:
        if actual != expected:
            raise InvalidTransitionError("This record changed. Reload before editing.")

    @staticmethod
    def _active(project: NativeProject) -> None:
        if project.status != "active":
            raise InvalidTransitionError("Restore the archived project before changing its board.")

    def _assignment(self, actor: ActorContext, project_id: UUID, value: TaskAssignment) -> None:
        if value.actor_id is None:
            return
        membership = self._membership(actor.workspace_id, value.actor_id)
        if (
            membership is None
            or "jobs:write" not in ROLE_SCOPES[membership.role]
            or self.store.native_project_member(actor.workspace_id, project_id, value.actor_id)
            is None
        ):
            raise InvalidTransitionError("Assignee must be an active project member who can work.")

    def _once(
        self,
        actor: ActorContext,
        resource: str,
        command: NativeCommand,
        result_type: type[Result],
        operation: Callable[[], Result],
    ) -> Result:
        output, _replayed = self.store.execute_once(
            f"native:{actor.workspace_id}:{actor.actor_id}:{resource}",
            command.idempotency_key,
            digest(command.model_dump(mode="json", exclude={"idempotency_key"})),
            lambda: operation().model_dump(mode="json"),
        )
        return result_type.model_validate(output)

    def _record(
        self,
        actor: ActorContext,
        event: str,
        record: NativeProject | NativeTask,
        *,
        detail: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "project_id": str(record.project_id if isinstance(record, NativeTask) else record.id),
            "version": record.version,
            "status": record.status,
            **(detail or {}),
        }
        if isinstance(record, NativeTask):
            payload["assignment"] = record.assignment.model_dump(mode="json")
        self.audit.record(
            event_type=f"native.{event}",
            actor=actor,
            resource_type="native_task" if isinstance(record, NativeTask) else "native_project",
            resource_id=str(record.id),
            payload=payload,
        )

    def list_projects(
        self, actor: ActorContext, offset: int = 0, limit: int = 50
    ) -> tuple[NativeProject, ...]:
        self._page(offset, limit)
        with self.store.transaction(actor.workspace_id):
            membership = self._workspace(actor)
            return self.store.native_projects(
                actor.workspace_id,
                actor.actor_id,
                offset,
                limit,
                all_projects=membership.role == "owner",
            )

    def get_project(self, actor: ActorContext, project_id: UUID) -> NativeProject:
        with self.store.transaction(actor.workspace_id):
            return self._project(actor, project_id)

    def create_project(self, actor: ActorContext, command: CreateNativeProject) -> NativeProject:
        # Existing identity changes share this lock. Keep this section to bounded DB work.
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._workspace(actor, write=True)

            def create() -> NativeProject:
                project = NativeProject(
                    workspace_id=actor.workspace_id,
                    created_by=actor.actor_id,
                    name=command.name,
                    objective=command.objective,
                )
                self.store.insert_native_project(project)
                self.store.put_native_project_member(
                    NativeProjectMember(
                        workspace_id=actor.workspace_id,
                        project_id=project.id,
                        actor_id=actor.actor_id,
                        role="owner",
                    )
                )
                self._record(actor, "project.created", project)
                return project

            result = self._once(actor, "project:create", command, NativeProject, create)
            # A revoked member cannot use the creation receipt to retrieve an old project.
            self._project(actor, result.id)
            return result

    def update_project(
        self, actor: ActorContext, project_id: UUID, command: UpdateNativeProject
    ) -> NativeProject:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._project(actor, project_id, write=True)

            def update() -> NativeProject:
                self._version(project.version, command.expected_version)
                if project.status != command.status:
                    self._project(actor, project_id, owner=True)
                result = project.model_copy(
                    update={
                        "name": command.name,
                        "objective": command.objective,
                        "status": command.status,
                        "version": project.version + 1,
                        "updated_at": utc_now(),
                    }
                )
                self.store.update_native_project(result, command.expected_version)
                self._record(actor, "project.updated", result)
                return result

            return self._once(actor, f"project:{project_id}:update", command, NativeProject, update)

    def members(self, actor: ActorContext, project_id: UUID) -> tuple[NativeProjectMember, ...]:
        with self.store.transaction(actor.workspace_id):
            self._project(actor, project_id)
            return self.store.native_project_members(actor.workspace_id, project_id)

    def _touch_project(
        self, actor: ActorContext, project: NativeProject, member_id: UUID, role: str | None
    ) -> NativeProject:
        result = project.model_copy(
            update={"version": project.version + 1, "updated_at": utc_now()}
        )
        self.store.update_native_project(result, project.version)
        self._record(
            actor,
            "project.members_changed",
            result,
            detail={"member_id": str(member_id), "role": role},
        )
        return result

    def _last_owner(self, project: NativeProject, member: NativeProjectMember) -> None:
        if member.role != "owner":
            return
        for candidate in self.store.native_project_members(project.workspace_id, project.id):
            if candidate.role != "owner" or candidate.actor_id == member.actor_id:
                continue
            active = self._membership(project.workspace_id, candidate.actor_id)
            if active is not None and "jobs:write" in ROLE_SCOPES[active.role]:
                return
        raise InvalidTransitionError("Add another project owner before removing this owner.")

    def put_member(
        self, actor: ActorContext, project_id: UUID, command: PutNativeProjectMember
    ) -> NativeProjectMember:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._project(actor, project_id, write=True, owner=True)

            def put() -> NativeProjectMember:
                self._active(project)
                self._version(project.version, command.expected_version)
                membership = self._membership(actor.workspace_id, command.actor_id)
                if membership is None:
                    raise InvalidTransitionError("Invite an active workspace member first.")
                if command.role == "owner" and "jobs:write" not in ROLE_SCOPES[membership.role]:
                    raise InvalidTransitionError(
                        "A read-only workspace member cannot own a project."
                    )
                prior = self.store.native_project_member(
                    actor.workspace_id, project_id, command.actor_id
                )
                if prior and prior.role != command.role:
                    self._last_owner(project, prior)
                result = NativeProjectMember(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    actor_id=command.actor_id,
                    role=command.role,
                    created_at=prior.created_at if prior else utc_now(),
                )
                self.store.put_native_project_member(result)
                self._touch_project(actor, project, command.actor_id, command.role)
                return result

            return self._once(
                actor, f"project:{project_id}:member", command, NativeProjectMember, put
            )

    def remove_member(
        self,
        actor: ActorContext,
        project_id: UUID,
        member_id: UUID,
        command: VersionedNativeCommand,
    ) -> NativeProject:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._project(actor, project_id, write=True, owner=True)

            def remove() -> NativeProject:
                self._active(project)
                self._version(project.version, command.expected_version)
                member = self.store.native_project_member(actor.workspace_id, project_id, member_id)
                if member is None:
                    raise NotFoundError("Project member not found.")
                self._last_owner(project, member)
                if self.store.native_assigned_task_exists(
                    actor.workspace_id, project_id, member_id
                ):
                    raise InvalidTransitionError(
                        "Reassign this member's tasks before removing them."
                    )
                self.store.delete_native_project_member(actor.workspace_id, project_id, member_id)
                return self._touch_project(actor, project, member_id, None)

            return self._once(
                actor, f"project:{project_id}:remove:{member_id}", command, NativeProject, remove
            )

    def tasks(
        self, actor: ActorContext, project_id: UUID, offset: int = 0, limit: int = 50
    ) -> tuple[NativeTask, ...]:
        self._page(offset, limit)
        with self.store.transaction(actor.workspace_id):
            self._project(actor, project_id)
            return self.store.native_tasks(actor.workspace_id, project_id, offset, limit)

    def get_task(self, actor: ActorContext, project_id: UUID, task_id: UUID) -> NativeTask:
        with self.store.transaction(actor.workspace_id):
            self._project(actor, project_id)
            task = self.store.native_task(actor.workspace_id, project_id, task_id)
            if task is None:
                raise NotFoundError("Task not found.")
            return task

    def create_task(
        self, actor: ActorContext, project_id: UUID, command: CreateNativeTask
    ) -> NativeTask:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._project(actor, project_id, write=True)

            def create() -> NativeTask:
                self._active(project)
                self._assignment(actor, project_id, command.assignment)
                result = NativeTask(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    created_by=actor.actor_id,
                    title=command.title,
                    description=command.description,
                    assignment=command.assignment,
                )
                self.store.insert_native_task(result)
                self._record(actor, "task.created", result)
                return result

            return self._once(actor, f"project:{project_id}:task", command, NativeTask, create)

    def update_task(
        self, actor: ActorContext, project_id: UUID, task_id: UUID, command: UpdateNativeTask
    ) -> NativeTask:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._project(actor, project_id, write=True)

            def update() -> NativeTask:
                self._active(project)
                task = self.get_task(actor, project_id, task_id)
                self._version(task.version, command.expected_version)
                self._assignment(actor, project_id, command.assignment)
                result = task.model_copy(
                    update={
                        "title": command.title,
                        "description": command.description,
                        "status": command.status,
                        "assignment": command.assignment,
                        "version": task.version + 1,
                        "updated_at": utc_now(),
                    }
                )
                self.store.update_native_task(result, command.expected_version)
                self._record(actor, "task.updated", result)
                return result

            return self._once(
                actor, f"project:{project_id}:task:{task_id}:update", command, NativeTask, update
            )

    def claim_task(
        self,
        actor: ActorContext,
        project_id: UUID,
        task_id: UUID,
        command: VersionedNativeCommand,
    ) -> NativeTask:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            project = self._project(actor, project_id, write=True)

            def claim() -> NativeTask:
                self._active(project)
                task = self.get_task(actor, project_id, task_id)
                self._version(task.version, command.expected_version)
                if task.assignment.kind != "pool" or task.status != "todo":
                    raise InvalidTransitionError("Only unclaimed to-do work can be claimed.")
                assignment = TaskAssignment(kind="human", actor_id=actor.actor_id)
                self._assignment(actor, project_id, assignment)
                result = task.model_copy(
                    update={
                        "assignment": assignment,
                        "status": "in_progress",
                        "version": task.version + 1,
                        "updated_at": utc_now(),
                    }
                )
                self.store.update_native_task(result, command.expected_version)
                self._record(actor, "task.claimed", result)
                return result

            return self._once(
                actor, f"project:{project_id}:task:{task_id}:claim", command, NativeTask, claim
            )
