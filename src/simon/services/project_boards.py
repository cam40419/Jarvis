"""Durable board mirroring with fenced writes and no uncertain-operation replay."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from simon.adapters.clickup import ClickUpAdapter, board_configuration_reasons, task_marker
from simon.domain.errors import (
    AuthorizationError,
    DomainError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Job, JobStatus, utc_now
from simon.domain.ports import Store
from simon.domain.project_board_state import (
    BindProjectBoard,
    BoardMapping,
    BoardOperation,
    ImportBoardTasks,
    ProjectBoardState,
    PublishBoardTasks,
    ReconcileBoardOperation,
)
from simon.domain.project_boards import BoardConnection, BoardList, BoardTask, BoardTaskPage
from simon.domain.project_work import ProjectActivityDraft, ProjectCycle, ProjectTodo
from simon.domain.tool_catalog import ToolExecutionError
from simon.services.canonical import digest
from simon.services.project_work import ProjectWorkService

BOARD_KIND = "platform.project_board"
BACKGROUND_BATCH = 5
STALE_OPERATION_SECONDS = 180


class ProjectBoardService:
    def __init__(
        self,
        store: Store,
        work: ProjectWorkService,
        adapter: ClickUpAdapter,
        connections: tuple[BoardConnection, ...],
        *,
        clock: Callable[[], datetime] = utc_now,
        sync_interval_seconds: int = 120,
    ) -> None:
        if not 60 <= sync_interval_seconds <= 3600:
            raise ValueError("Board polling interval must be between 60 and 3600 seconds")
        self.store, self.work, self.adapter = store, work, adapter
        self.configured_connections, self.clock = connections, clock
        self.sync_interval_seconds = sync_interval_seconds
        self.work.todo_edit_validator = self._validate_edit

    def _access(self, actor: ActorContext, project_id: UUID, *, write: bool = False) -> None:
        self.work.authorize(actor, write=write)
        self.work.project_resolver(actor, project_id)

    @staticmethod
    def _expected(state: ProjectBoardState, version: int) -> None:
        if state.version != version:
            raise InvalidTransitionError("Project board changed; refresh before saving")

    @staticmethod
    def _id(actor: ActorContext, project_id: UUID) -> UUID:
        return uuid5(project_id, f"board:{actor.household_id}:{actor.actor_id}")

    @staticmethod
    def _op_kind(project_id: UUID) -> str:
        return "platform.board_operation." + project_id.hex

    @staticmethod
    def _view(job: Job) -> ProjectBoardState:
        return ProjectBoardState.model_validate(
            job.result or job.input["initial_state"]
        ).model_copy(
            update={"version": job.version},
        )

    def _job(self, actor: ActorContext, project_id: UUID, *, create: bool = False) -> Job | None:
        self._access(actor, project_id)
        identifier = self._id(actor, project_id)
        job = self.store.get_job(identifier)
        if job and (
            job.kind != BOARD_KIND
            or job.created_by != actor.actor_id
            or job.household_id != actor.household_id
        ):
            raise NotFoundError("Project board not found")
        if job is None and create:
            initial = self.get(actor, project_id)
            job, _ = self.store.create_job(
                Job(
                    id=identifier,
                    household_id=actor.household_id,
                    created_by=actor.actor_id,
                    kind=BOARD_KIND,
                    idempotency_key=identifier.hex,
                    status=JobStatus.WAITING,
                    input={
                        "project_id": str(project_id),
                        "scopes": sorted(actor.scopes),
                        "initial_state": initial.model_dump(mode="json"),
                    },
                    input_digest=digest(str(project_id)),
                )
            )
        return job

    def get(self, actor: ActorContext, project_id: UUID) -> ProjectBoardState:
        job = self._job(actor, project_id)
        return (
            self._view(job)
            if job
            else ProjectBoardState(
                project_id=project_id,
                workspace_id=actor.household_id,
                actor_id=actor.actor_id,
                updated_at=self.clock(),
            )
        )

    def _save(self, job: Job, state: ProjectBoardState) -> ProjectBoardState:
        state = ProjectBoardState.model_validate(state.model_dump(mode="python")).model_copy(
            update={"updated_at": self.clock()},
        )
        status = JobStatus.NEEDS_HUMAN if state.blocked_reasons else JobStatus.QUEUED
        if (
            state.blocked_reasons
            and 0 < state.read_failure_count < 4
            and not state.pending_operation_ids
        ):
            status = JobStatus.QUEUED
        return self._view(
            self.store.save_job(
                job.model_copy(
                    update={
                        "result": state.model_dump(mode="json"),
                        "status": status,
                        "input": {
                            **job.input,
                            "rank": int(state.next_sync_at.timestamp())
                            if state.next_sync_at
                            else 0,
                        },
                        "updated_at": self.clock(),
                    }
                ),
                job.version,
            )
        )

    def _connection(self, actor: ActorContext, identifier: str) -> BoardConnection:
        self.work.authorize(actor)
        connection = next(
            (
                item
                for item in self.configured_connections
                if item.id == identifier
                and item.household_id == actor.household_id
                and actor.actor_id in item.actor_ids
            ),
            None,
        )
        if connection is None:
            raise NotFoundError("Project board connection not found")
        return connection

    def connections(self, actor: ActorContext) -> list[dict[str, Any]]:
        self.work.authorize(actor)
        return [
            {
                "id": item.id,
                "name": item.name,
                "provider": item.provider,
                "list_ids": sorted(item.list_ids),
                "available": not board_configuration_reasons(item, self.adapter.http.environ),
                "blocked_reasons": board_configuration_reasons(item, self.adapter.http.environ),
            }
            for item in self.configured_connections
            if item.household_id == actor.household_id and actor.actor_id in item.actor_ids
        ]

    def boards(self, actor: ActorContext, connection_id: str) -> tuple[BoardList, ...]:
        return self.adapter.boards(self._connection(actor, connection_id), actor)

    def _bound(
        self, actor: ActorContext, project_id: UUID
    ) -> tuple[ProjectBoardState, BoardConnection]:
        state = self.get(actor, project_id)
        if state.binding is None:
            raise ValidationError("Connect this project to a board first")
        connection = self._connection(actor, state.binding.connection_id)
        if state.binding.list_id not in connection.list_ids:
            raise AuthorizationError("The bound list is no longer authorized")
        return state, connection

    def bind(
        self,
        actor: ActorContext,
        project_id: UUID,
        request: BindProjectBoard,
    ) -> ProjectBoardState:
        self._access(actor, project_id, write=True)
        connection = self._connection(actor, request.binding.connection_id)
        board = self.adapter.list_metadata(connection, actor, request.binding.list_id)
        if not set(request.binding.status_map.values()) <= {item.status for item in board.statuses}:
            raise ValidationError("Choose statuses that exist on the selected board")
        with self.store.transaction(actor.household_id):
            current = self.get(actor, project_id)
            self._expected(current, request.expected_version)
            if current.pending_operation_ids or self.work.get(actor, project_id).active_cycle:
                raise InvalidTransitionError("Settle board operations and the active cycle first")
            if (
                current.mappings
                and current.binding
                and (current.binding.connection_id, current.binding.list_id)
                != (request.binding.connection_id, request.binding.list_id)
            ):
                raise InvalidTransitionError(
                    "Existing task mappings cannot be moved to another board"
                )
            job = self._job(actor, project_id, create=True)
            assert job is not None
            return self._save(
                job,
                current.model_copy(
                    update={
                        "binding": request.binding,
                        "board": board,
                        "blocked_reasons": (),
                        "next_sync_at": self.clock()
                        + timedelta(seconds=self.sync_interval_seconds),
                    }
                ),
            )

    def preview(self, actor: ActorContext, project_id: UUID, page: int = 0) -> BoardTaskPage:
        state, connection = self._bound(actor, project_id)
        assert state.binding
        return self.adapter.tasks(connection, actor, state.binding.list_id, page=page)

    @staticmethod
    def _fingerprint(task: BoardTask) -> str:
        value = task.model_dump(mode="json", exclude={"date_updated", "url"})
        value["dependencies"] = sorted(value["dependencies"])
        value["assignees"] = sorted(value["assignees"], key=lambda item: item["id"])
        return digest(value)

    @classmethod
    def _mapping(
        cls, todo_id: str, task: BoardTask, old: BoardMapping | None = None
    ) -> BoardMapping:
        return BoardMapping(
            todo_id=todo_id,
            remote_id=task.id,
            url=task.url,
            date_updated=task.date_updated,
            baseline_hash=cls._fingerprint(task),
            remote_status=task.status,
            remote_archived=task.archived,
            last_published_status=old.last_published_status
            if old and old.remote_status == task.status
            else None,
            last_status_digest=old.last_status_digest
            if old and old.remote_status == task.status
            else None,
            last_progress_digest=old.last_progress_digest if old else None,
        )

    def is_mapped(self, actor: ActorContext, project_id: UUID, todo_id: str) -> bool:
        return any(item.todo_id == todo_id for item in self.get(actor, project_id).mappings)

    def _validate_edit(
        self,
        actor: ActorContext,
        project_id: UUID,
        old: ProjectTodo,
        new: ProjectTodo,
    ) -> None:
        state = self.get(actor, project_id)
        pending = any(
            item.todo_id == old.id and item.state != "succeeded"
            for item in self.operations(actor, project_id)
            if item.id in state.pending_operation_ids
        )
        if (pending or any(item.todo_id == old.id for item in state.mappings)) and any(
            getattr(old, key) != getattr(new, key)
            for key in ("title", "objective", "depends_on", "status")
        ):
            raise ValidationError("Edit mapped task details, dependencies and status in ClickUp")

    @staticmethod
    def _remote_status(task: BoardTask, state: ProjectBoardState) -> str:
        assert state.binding
        if task.archived:
            return "cancelled"
        if task.status_type in {"closed", "done"}:
            return "done"
        if task.status == state.binding.status_map.get("blocked"):
            return "blocked"
        return "todo"

    def _import_rows(
        self,
        actor: ActorContext,
        project_id: UUID,
        state: ProjectBoardState,
        tasks: tuple[BoardTask, ...],
        *,
        allow_starting: bool = False,
    ) -> ProjectBoardState:
        work_job = self.work._job(actor, project_id, create=True)
        board_job = self._job(actor, project_id)
        assert work_job and board_job
        work = self.work.view(work_job)
        if work.active_cycle and not (allow_starting and work.active_cycle.phase == "starting"):
            raise InvalidTransitionError(
                "Wait until active planning and execution settle before importing"
            )
        rows = {todo.id: todo for todo in work.todos}
        mappings = {item.todo_id: item for item in state.mappings}
        remote_ids = {item.remote_id: item.todo_id for item in state.mappings}
        for task in tasks:
            remote_ids.setdefault(task.id, "board-" + uuid5(board_job.id, task.id).hex)
        updates = []
        for task in tasks:
            identifier = remote_ids[task.id]
            old = rows.get(identifier)
            if any(key not in remote_ids for key in task.dependencies):
                raise ValidationError("Import each external prerequisite before its dependent task")
            status = self._remote_status(task, state)
            previous = mappings.get(identifier)
            # Provider comments/timestamps and unrelated metadata changes must never
            # reschedule completed or uncertain execution. A provider status transition
            # is an explicit business change; unknown outcomes still require recovery.
            if (
                old
                and previous
                and (
                    (not task.archived and task.status == previous.remote_status)
                    or old.status == "unknown"
                )
            ):
                status = old.status
            changes = {
                "title": task.name[:240],
                "objective": (task.description or task.name)[:16000],
                "depends_on": tuple(remote_ids[key] for key in task.dependencies),
                "status": status,
                "progress": old.progress
                if old and status == old.status
                else (100 if status == "done" else 0),
                "updated_at": self.clock(),
            }
            todo = (
                old.model_copy(update=changes)
                if old
                else ProjectTodo.model_validate(
                    {
                        "id": identifier,
                        **changes,
                        "created_at": self.clock(),
                    }
                )
            )
            updates.append(todo)
            mappings[identifier] = self._mapping(identifier, task, mappings.get(identifier))
        self.work._save(
            work_job,
            work.model_copy(
                update={
                    "todos": self.work._todos(work, tuple(updates)),
                }
            ),
        )
        return self._save(
            board_job,
            state.model_copy(
                update={
                    "mappings": tuple(mappings.values()),
                    "last_sync_at": self.clock(),
                    "blocked_reasons": (),
                }
            ),
        )

    def import_tasks(
        self,
        actor: ActorContext,
        project_id: UUID,
        request: ImportBoardTasks,
    ) -> ProjectBoardState:
        self._access(actor, project_id, write=True)
        state, connection = self._bound(actor, project_id)
        assert state.binding
        tasks = self.adapter.get_tasks(
            connection, actor, tuple(dict.fromkeys(request.task_ids)), list_id=state.binding.list_id
        )
        with self.store.transaction(actor.household_id):

            def operation() -> dict[str, Any]:
                current = self.get(actor, project_id)
                self._expected(current, request.expected_version)
                if current.binding != state.binding:
                    raise InvalidTransitionError("Board binding changed during import")
                self._guard(current)
                self._import_rows(actor, project_id, current, tasks)
                return {"imported": [task.id for task in tasks]}

            self.store.execute_once(
                f"board-import:{self._id(actor, project_id)}",
                request.idempotency_key,
                digest(request.model_dump(mode="json")),
                operation,
            )
        return self.get(actor, project_id)

    @staticmethod
    def _guard(state: ProjectBoardState) -> None:
        if state.pending_operation_ids or state.blocked_reasons:
            raise ValidationError("Review pending, uncertain or conflicting board operations first")

    @staticmethod
    def _op_view(job: Job) -> BoardOperation:
        return BoardOperation.model_validate(job.result or job.input["initial_state"])

    def operations(self, actor: ActorContext, project_id: UUID) -> tuple[BoardOperation, ...]:
        self._access(actor, project_id)
        return tuple(
            self._op_view(job)
            for job in self.store.jobs(
                actor.household_id,
                actor.actor_id,
                self._op_kind(project_id),
                0,
                100,
            )
        )

    def _operation(
        self,
        actor: ActorContext,
        project_id: UUID,
        *,
        kind: str,
        todo_id: str,
        key: str,
        payload: dict[str, Any],
        send: Callable[[str], tuple[dict[str, Any], BoardTask | None]],
        expected_binding: str,
        cycle_id: UUID | None = None,
    ) -> BoardOperation:
        identifier = uuid5(self._id(actor, project_id), "operation:" + key)
        fingerprint = digest({"kind": kind, "todo_id": todo_id, "payload": payload})
        with self.store.transaction(actor.household_id):
            self._access(actor, project_id, write=True)
            existing = self.store.get_job(identifier)
            if existing:
                if existing.input_digest != fingerprint:
                    raise IdempotencyConflictError("Board operation key has different content")
                result = self._op_view(existing)
                if result.state == "succeeded":
                    return result
                raise InvalidTransitionError(
                    "This board operation requires review; it will not be replayed"
                )
            job = self._job(actor, project_id)
            assert job is not None
            state = self._view(job)
            self._guard(state)
            self._write_fence(actor, state, expected_binding, cycle_id)
            if kind == "create":
                if any(item.todo_id == todo_id for item in state.mappings):
                    raise InvalidTransitionError("Task was already published; refresh its mapping")
                todo = next(
                    (item for item in self.work.get(actor, project_id).todos if item.id == todo_id),
                    None,
                )
                if todo is None or (todo.title, todo.objective) != (
                    payload["name"],
                    payload["description"],
                ):
                    raise InvalidTransitionError("Local task changed before board publication")
            operation = BoardOperation.model_validate(
                {
                    "id": identifier,
                    "project_id": project_id,
                    "kind": kind,
                    "todo_id": todo_id,
                    "marker": identifier.hex,
                    "payload": payload,
                    "claim_id": uuid4(),
                    "remote_id": payload.get("remote_id"),
                    "created_at": self.clock(),
                    "updated_at": self.clock(),
                }
            )
            self.store.create_job(
                Job(
                    id=identifier,
                    household_id=actor.household_id,
                    created_by=actor.actor_id,
                    kind=self._op_kind(project_id),
                    idempotency_key=identifier.hex,
                    input={
                        "initial_state": operation.model_dump(mode="json"),
                        "rank": -int(self.clock().timestamp() * 1000000),
                    },
                    input_digest=fingerprint,
                    status=JobStatus.RUNNING,
                )
            )
            self._save(job, state.model_copy(update={"pending_operation_ids": (identifier,)}))
        # The durable claim commits before any provider call. No automatic retries.
        task = None
        try:
            # Re-resolve current membership, scopes, project and binding immediately
            # before dispatch. Binding edits cannot pass while this claim is pending.
            live_actor = self.work.live_actor(job)
            self._write_fence(
                live_actor, self.get(live_actor, project_id), expected_binding, cycle_id
            )
            receipt, task = send(operation.marker)
            operation = operation.model_copy(
                update={
                    "state": "succeeded",
                    "receipt": receipt,
                    "remote_id": task.id if task else operation.remote_id,
                }
            )
        except Exception as error:
            unknown = not isinstance(error, DomainError) or (
                isinstance(error, ToolExecutionError) and error.unknown
            )
            operation = operation.model_copy(
                update={
                    "state": "unknown" if unknown else "failed",
                    "error": "Board outcome is unknown; inspect the provider before reconciliation."
                    if unknown
                    else "Board write was rejected or its precondition changed; review it.",
                }
            )
        with self.store.transaction(actor.household_id):
            row = self.store.get_job(identifier)
            # Recording a receipt remains necessary even if access was revoked during
            # the provider call. These identifiers were owner-checked before dispatch.
            job = self.store.get_job(self._id(actor, project_id))
            assert row and job
            if self._op_view(row).claim_id != operation.claim_id:
                raise InvalidTransitionError(
                    "Board operation was reconciled while its worker was running"
                )
            operation = operation.model_copy(update={"updated_at": self.clock()})
            self.store.save_job(
                row.model_copy(
                    update={
                        "result": operation.model_dump(mode="json"),
                        "status": JobStatus.SUCCEEDED
                        if operation.state == "succeeded"
                        else JobStatus.NEEDS_HUMAN,
                    }
                ),
                row.version,
            )
            state = self._view(job)
            mappings = {item.todo_id: item for item in state.mappings}
            if task is not None and operation.state == "succeeded":
                mappings[todo_id] = self._mapping(todo_id, task, mappings.get(todo_id))
            self._save(
                job,
                state.model_copy(
                    update={
                        "mappings": tuple(mappings.values()),
                        "abandoned_create_todo_ids": tuple(
                            item
                            for item in state.abandoned_create_todo_ids
                            if item != todo_id or kind != "create" or operation.state != "succeeded"
                        ),
                        "pending_operation_ids": ()
                        if operation.state == "succeeded"
                        else (identifier,),
                        "blocked_reasons": ()
                        if operation.state == "succeeded"
                        else (operation.error,),
                    }
                ),
            )
        if operation.state != "succeeded":
            raise ValidationError(operation.error or "Board operation requires review")
        return operation

    def _check_remote(
        self,
        actor: ActorContext,
        state: ProjectBoardState,
        connection: BoardConnection,
        mapping: BoardMapping,
    ) -> BoardTask:
        assert state.binding
        remote = self.adapter.get_task(
            connection, actor, mapping.remote_id, list_id=state.binding.list_id
        )
        if (
            remote.date_updated != mapping.date_updated
            or self._fingerprint(remote) != mapping.baseline_hash
        ):
            raise InvalidTransitionError(
                "ClickUp task changed; synchronize and review before execution or writes"
            )
        return remote

    def publish(
        self,
        actor: ActorContext,
        project_id: UUID,
        request: PublishBoardTasks,
        *,
        _cycle_id: UUID | None = None,
    ) -> ProjectBoardState:
        self._access(actor, project_id, write=True)
        state, connection = self._bound(actor, project_id)
        assert state.binding and state.board
        # Save admission once, so retries of a partially completed publish keep their identity.
        with self.store.transaction(actor.household_id):

            def admit() -> dict[str, Any]:
                current = self.get(actor, project_id)
                self._expected(current, request.expected_version)
                self._guard(current)
                return {
                    "binding": current.binding.model_dump(mode="json") if current.binding else None
                }

            admitted, _ = self.store.execute_once(
                f"board-publish:{self._id(actor, project_id)}",
                request.idempotency_key,
                digest(request.model_dump(mode="json", exclude={"expected_version"})),
                admit,
            )
        state, connection = self._bound(actor, project_id)
        assert state.binding and state.board
        if admitted["binding"] != state.binding.model_dump(mode="json"):
            raise InvalidTransitionError("Board binding changed since this publish request")
        self._guard(state)
        local = {todo.id: todo for todo in self.work.get(actor, project_id).todos}
        selected = tuple(dict.fromkeys(request.todo_ids))
        mapped = {item.todo_id: item for item in state.mappings}
        if any(key not in local for key in selected):
            raise NotFoundError("Project task to publish was not found")
        if any(
            dep not in mapped and dep not in selected
            for key in selected
            for dep in local[key].depends_on
        ):
            raise ValidationError("Select each unpublished prerequisite with its dependent task")
        status = state.binding.status_map.get("ready") or next(
            (item.status for item in state.board.statuses if item.type not in {"closed", "done"}),
            "",
        )
        if not status:
            raise ValidationError("Choose a ready status before publishing tasks")
        for identifier in selected:
            if identifier in mapped:
                self._check_remote(actor, state, connection, mapped[identifier])
                continue
            todo = local[identifier]
            task_status = (
                state.binding.status_map.get("done", status) if todo.status == "done" else status
            )
            payload = {
                "name": todo.title,
                "description": todo.objective,
                "status": task_status,
                "list_id": state.binding.list_id,
            }

            def create(
                marker: str, body: dict[str, Any] = payload
            ) -> tuple[dict[str, Any], BoardTask]:
                remote = self.adapter.create_task(
                    connection,
                    actor,
                    body["list_id"],
                    name=body["name"],
                    description=body["description"],
                    status=body["status"],
                    marker=marker,
                )
                return {"task_id": remote.id, "url": remote.url}, remote

            self._operation(
                actor,
                project_id,
                kind="create",
                todo_id=identifier,
                key=f"{request.idempotency_key}:create:{identifier}",
                payload=payload,
                send=create,
                expected_binding=self._signature(state, connection),
                cycle_id=_cycle_id,
            )
            state = self.get(actor, project_id)
            mapped = {item.todo_id: item for item in state.mappings}
        for identifier in selected:
            for dependency in local[identifier].depends_on:
                state = self.get(actor, project_id)
                assert state.binding
                mapped = {item.todo_id: item for item in state.mappings}
                before = self._check_remote(actor, state, connection, mapped[identifier])
                target = mapped[dependency].remote_id
                if target in before.dependencies:
                    continue
                payload = {"remote_id": before.id, "depends_on": target}

                def relate(
                    marker: str,
                    body: dict[str, Any] = payload,
                    previous: BoardTask = before,
                    bound: ProjectBoardState = state,
                ) -> tuple[dict[str, Any], BoardTask]:
                    assert bound.binding
                    receipt = self.adapter.add_dependency(
                        connection,
                        actor,
                        body["remote_id"],
                        body["depends_on"],
                        list_id=bound.binding.list_id,
                    )
                    try:
                        remote = self.adapter.get_task(
                            connection, actor, previous.id, list_id=bound.binding.list_id
                        )
                        expected = previous.model_copy(
                            update={
                                "dependencies": (*previous.dependencies, body["depends_on"]),
                            }
                        )
                        if set(remote.dependencies) != set(
                            expected.dependencies
                        ) or self._fingerprint(
                            remote.model_copy(update={"dependencies": expected.dependencies})
                        ) != self._fingerprint(expected):
                            raise ValueError
                    except Exception:
                        raise ToolExecutionError(
                            "Dependency write needs reconciliation", unknown=True
                        ) from None
                    return receipt, remote

                self._operation(
                    actor,
                    project_id,
                    kind="dependency",
                    todo_id=identifier,
                    key=f"{request.idempotency_key}:dependency:{identifier}:{dependency}",
                    payload=payload,
                    send=relate,
                    expected_binding=self._signature(state, connection),
                    cycle_id=_cycle_id,
                )
        return self.get(actor, project_id)

    @staticmethod
    def _signature(state: ProjectBoardState, connection: BoardConnection) -> str:
        return digest(
            {
                "binding": state.binding.model_dump(mode="json") if state.binding else None,
                "connection": connection.model_dump(mode="json"),
            }
        )

    def _write_fence(
        self,
        actor: ActorContext,
        state: ProjectBoardState,
        expected_binding: str,
        cycle_id: UUID | None,
    ) -> None:
        self._access(actor, state.project_id, write=True)
        if state.binding is None:
            raise InvalidTransitionError("Board binding changed before dispatch")
        connection = self._connection(actor, state.binding.connection_id)
        if self._signature(state, connection) != expected_binding:
            raise InvalidTransitionError("Board binding or connection changed before dispatch")
        if state.binding.list_id not in connection.list_ids or board_configuration_reasons(
            connection, self.adapter.http.environ
        ):
            raise AuthorizationError("Board connection is no longer available")
        work = self.work.get(actor, state.project_id)
        if cycle_id is not None and (
            work.autonomy.paused
            or not work.active_cycle
            or work.active_cycle.id != cycle_id
            or work.active_cycle.phase != "ready"
            or not (work.active_cycle.automatic or work.active_cycle.execution_approved)
        ):
            raise InvalidTransitionError("Project cycle changed or paused before board dispatch")

    def _set_error(self, job: Job, message: str, *, retry_read: bool = False) -> None:
        with self.store.transaction(job.household_id):
            current = self.store.get_job(job.id)
            if current is not None:
                state = self._view(current)
                failures = min(state.read_failure_count + 1, 4) if retry_read else 0
                self._save(
                    current,
                    state.model_copy(
                        update={
                            "blocked_reasons": (message,),
                            "read_failure_count": failures,
                            "next_sync_at": self.clock()
                            + timedelta(seconds=self.sync_interval_seconds * 2**failures),
                        }
                    ),
                )

    def _sync(
        self,
        actor: ActorContext,
        project_id: UUID,
        *,
        expected_version: int | None = None,
        allow_starting: bool = False,
    ) -> ProjectBoardState:
        self._access(actor, project_id, write=True)
        state, connection = self._bound(actor, project_id)
        assert state.binding
        if expected_version is not None:
            self._expected(state, expected_version)
        if state.pending_operation_ids:
            raise ValidationError("Reconcile pending board writes before synchronizing")
        work = self.work.get(actor, project_id)
        if work.active_cycle and not (allow_starting and work.active_cycle.phase == "starting"):
            raise InvalidTransitionError(
                "Wait until active planning and execution settle before synchronizing"
            )
        # A rotating, five-task batch bounds provider requests and dispatcher latency.
        count = len(state.mappings)
        start = state.mapping_cursor % count if count else 0
        chosen = (state.mappings[start:] + state.mappings[:start])[:BACKGROUND_BATCH]
        tasks = (
            self.adapter.get_tasks(
                connection,
                actor,
                tuple(item.remote_id for item in chosen),
                list_id=state.binding.list_id,
            )
            if chosen
            else ()
        )
        with self.store.transaction(actor.household_id):
            current = self.get(actor, project_id)
            self._expected(current, state.version)
            if current.pending_operation_ids:
                raise InvalidTransitionError("Board write started during synchronization")
            current = self._import_rows(
                actor, project_id, current, tasks, allow_starting=allow_starting
            )
            row = self._job(actor, project_id)
            assert row
            return self._save(
                row,
                current.model_copy(
                    update={
                        "mapping_cursor": (start + len(chosen)) % count if count else 0,
                        "next_sync_at": self.clock()
                        + timedelta(seconds=self.sync_interval_seconds),
                    }
                ),
            )

    def sync(
        self,
        actor: ActorContext,
        project_id: UUID,
        *,
        expected_version: int | None = None,
    ) -> ProjectBoardState:
        self._sync(actor, project_id, expected_version=expected_version)
        self._push_one(actor, project_id)
        self._reset_read_failures(actor, project_id)
        return self.get(actor, project_id)

    def _reset_read_failures(self, actor: ActorContext, project_id: UUID) -> None:
        with self.store.transaction(actor.household_id):
            row = self._job(actor, project_id)
            assert row
            state = self._view(row)
            if state.read_failure_count and not state.pending_operation_ids:
                self._save(row, state.model_copy(update={"read_failure_count": 0}))

    def prepare(self, actor: ActorContext, project_id: UUID) -> None:
        state = self.get(actor, project_id)
        if state.binding is None:
            return
        self._guard(state)
        self._sync(actor, project_id, allow_starting=True)

    def before_execution(self, actor: ActorContext, project_id: UUID, cycle: ProjectCycle) -> None:
        state = self.get(actor, project_id)
        if state.binding is None:
            return
        self._guard(state)
        connection = self._connection(actor, state.binding.connection_id)
        self._write_fence(actor, state, self._signature(state, connection), cycle.id)
        work = self.work.get(actor, project_id)
        rows = {item.id: item for item in work.todos}
        selected = {item.id for item in work.todos if item.cycle_id == cycle.id}
        queue = list(selected)
        while queue:
            for dependency in rows[queue.pop()].depends_on:
                if dependency not in selected:
                    selected.add(dependency)
                    queue.append(dependency)
        if len(selected) > 25:
            raise ValidationError(
                "A board-backed execution supports at most 25 tasks including prerequisites"
            )
        if state.binding.auto_publish and selected:
            if selected.intersection(state.abandoned_create_todo_ids):
                raise ValidationError(
                    "An abandoned creation requires a new explicit Publish request"
                )
            state = self.publish(
                actor,
                project_id,
                PublishBoardTasks(
                    todo_ids=tuple(sorted(selected)),
                    expected_version=state.version,
                    idempotency_key=f"cycle-{cycle.id}-publish",
                ),
                _cycle_id=cycle.id,
            )
        assert state.binding
        connection = self._connection(actor, state.binding.connection_id)
        mappings = tuple(item for item in state.mappings if item.todo_id in selected)
        if mappings:
            remote = self.adapter.get_tasks(
                connection,
                actor,
                tuple(item.remote_id for item in mappings),
                list_id=state.binding.list_id,
            )
            observed = {item.id: item for item in remote}
            if any(
                observed[item.remote_id].archived
                or observed[item.remote_id].date_updated != item.date_updated
                or self._fingerprint(observed[item.remote_id]) != item.baseline_hash
                for item in mappings
            ):
                raise ValidationError(
                    "ClickUp tasks changed during planning; synchronize and review a new plan"
                )
        self._write_fence(
            actor,
            self.get(actor, project_id),
            self._signature(state, connection),
            cycle.id,
        )

    @staticmethod
    def _progress(todo: ProjectTodo) -> tuple[str, str]:
        value = {
            "status": todo.status,
            "progress": todo.progress,
            "result": todo.result,
            "error": todo.error,
            "run_id": str(todo.run_id) if todo.run_id else None,
        }
        text = f"Simon execution: {todo.status} ({todo.progress}%)."
        if todo.run_id:
            text += f"\nRun: {todo.run_id}"
        if todo.result:
            text += "\n" + todo.result[:3000]
        if todo.error:
            text += "\nReview needed: " + todo.error[:1000]
        return digest(value), text

    def _update_mapping_flags(
        self,
        actor: ActorContext,
        project_id: UUID,
        todo_id: str,
        **changes: Any,
    ) -> None:
        with self.store.transaction(actor.household_id):
            row = self._job(actor, project_id)
            assert row
            state = self._view(row)
            self._save(
                row,
                state.model_copy(
                    update={
                        "mappings": tuple(
                            item.model_copy(update=changes) if item.todo_id == todo_id else item
                            for item in state.mappings
                        )
                    }
                ),
            )

    def _push_one(self, actor: ActorContext, project_id: UUID) -> bool:
        state, connection = self._bound(actor, project_id)
        self._guard(state)
        assert state.binding
        if not (state.binding.sync_progress or state.binding.sync_status):
            return False
        rows = {item.id: item for item in self.work.get(actor, project_id).todos}
        for mapping in state.mappings:
            todo = rows.get(mapping.todo_id)
            if (
                mapping.remote_archived
                or todo is None
                or todo.status in {"todo", "ready", "archived"}
            ):
                continue
            progress_hash, text = self._progress(todo)
            desired = state.binding.status_map.get(
                "blocked" if todo.status in {"unknown", "cancelled"} else todo.status,  # type: ignore[arg-type]
            )
            if (
                state.binding.sync_status
                and desired
                and desired != mapping.remote_status
                and mapping.last_status_digest != progress_hash
            ):
                assert desired is not None
                before = self._check_remote(actor, state, connection, mapping)
                payload = {
                    "remote_id": mapping.remote_id,
                    "status": desired,
                    "expected_updated": before.date_updated,
                    "progress_hash": progress_hash,
                }

                def status_write(
                    marker: str,
                    previous: BoardTask = before,
                    status: str | None = desired,
                ) -> tuple[dict[str, Any], BoardTask]:
                    assert state.binding
                    assert status
                    remote = self.adapter.update_status(
                        connection,
                        actor,
                        previous.id,
                        list_id=state.binding.list_id,
                        status=status,
                        expected_updated=previous.date_updated,
                    )
                    return {"task_id": remote.id, "status": remote.status}, remote

                self._operation(
                    actor,
                    project_id,
                    kind="status",
                    todo_id=todo.id,
                    key=f"status:{todo.id}:{progress_hash}",
                    payload=payload,
                    send=status_write,
                    expected_binding=self._signature(state, connection),
                )
                self._update_mapping_flags(
                    actor,
                    project_id,
                    todo.id,
                    last_published_status=desired,
                    last_status_digest=progress_hash,
                )
                return True
            if state.binding.sync_progress and mapping.last_progress_digest != progress_hash:
                before = self._check_remote(actor, state, connection, mapping)
                payload = {
                    "remote_id": mapping.remote_id,
                    "text": text,
                    "progress_hash": progress_hash,
                }

                def comment_write(
                    marker: str,
                    previous: BoardTask = before,
                    content: str = text,
                ) -> tuple[dict[str, Any], BoardTask]:
                    assert state.binding
                    receipt = self.adapter.append_comment(
                        connection,
                        actor,
                        previous.id,
                        list_id=state.binding.list_id,
                        text=content,
                        marker=marker,
                    )
                    try:
                        remote = self.adapter.get_task(
                            connection, actor, previous.id, list_id=state.binding.list_id
                        )
                        if self._fingerprint(remote) != self._fingerprint(previous):
                            raise ValueError
                    except Exception:
                        raise ToolExecutionError(
                            "Comment needs reconciliation", unknown=True
                        ) from None
                    return receipt, remote

                self._operation(
                    actor,
                    project_id,
                    kind="comment",
                    todo_id=todo.id,
                    key=f"comment:{todo.id}:{progress_hash}",
                    payload=payload,
                    send=comment_write,
                    expected_binding=self._signature(state, connection),
                )
                self._update_mapping_flags(
                    actor, project_id, todo.id, last_progress_digest=progress_hash
                )
                return True
        return False

    def _recover_stale(self, job: Job) -> None:
        with self.store.transaction(job.household_id):
            current = self.store.get_job(job.id)
            assert current
            state = self._view(current)
            for identifier in state.pending_operation_ids:
                row = self.store.get_job(identifier)
                if row is None:
                    continue
                operation = self._op_view(row)
                if (
                    operation.state == "running"
                    and (self.clock() - operation.created_at).total_seconds()
                    >= STALE_OPERATION_SECONDS
                ):
                    operation = operation.model_copy(
                        update={
                            "state": "unknown",
                            "updated_at": self.clock(),
                            "error": "Interrupted write requires inspection and reconciliation.",
                        }
                    )
                    self.store.save_job(
                        row.model_copy(
                            update={
                                "result": operation.model_dump(mode="json"),
                                "status": JobStatus.NEEDS_HUMAN,
                            }
                        ),
                        row.version,
                    )
                    self._save(
                        current, state.model_copy(update={"blocked_reasons": (operation.error,)})
                    )
                    return

    def tick(self) -> int:
        candidates = self.store.jobs_all(BOARD_KIND, 1, JobStatus.QUEUED.value)
        if not candidates:
            return 0
        row = candidates[0]
        state = self._view(row)
        if state.next_sync_at and state.next_sync_at > self.clock():
            return 0
        with self.store.transaction(row.household_id):
            current = self.store.get_job(row.id)
            if current is None or current.version != row.version:
                return 0
            self._save(
                current,
                state.model_copy(
                    update={
                        "next_sync_at": self.clock()
                        + timedelta(seconds=self.sync_interval_seconds),
                    }
                ),
            )
        try:
            actor = self.work.live_actor(row)
            if state.pending_operation_ids:
                self._recover_stale(row)
                return 1
            work = self.work.get(actor, state.project_id)
            if work.active_cycle is None:
                self._sync(actor, state.project_id)
            self._push_one(actor, state.project_id)
            self._reset_read_failures(actor, state.project_id)
        except ToolExecutionError as error:
            # Only reads escape as ToolExecutionError: _operation records all write
            # failures and rethrows ValidationError, so uncertain writes never replay.
            self._set_error(row, str(error)[:1000], retry_read=True)
        except DomainError as error:
            self._set_error(row, str(error)[:1000])
        except Exception:
            self._set_error(
                row,
                "Board synchronization stopped; inspect its operations and refresh after review.",
            )
        return 1

    def reconcile(
        self,
        actor: ActorContext,
        project_id: UUID,
        request: ReconcileBoardOperation,
    ) -> ProjectBoardState:
        self._access(actor, project_id, write=True)
        state, connection = self._bound(actor, project_id)
        self._expected(state, request.expected_version)
        row = self.store.get_job(request.operation_id)
        if (
            row is None
            or row.household_id != actor.household_id
            or row.created_by != actor.actor_id
            or row.kind != self._op_kind(project_id)
            or row.id not in state.pending_operation_ids
        ):
            raise NotFoundError("Pending board operation not found")
        operation = self._op_view(row)
        if (
            operation.state == "running"
            and (self.clock() - operation.created_at).total_seconds() < STALE_OPERATION_SECONDS
        ):
            raise InvalidTransitionError(
                "Allow the active board request to settle before reconciliation"
            )
        assert state.binding
        if operation.kind == "create" and request.resolution == "acknowledge":
            return self._abandon_create(actor, project_id, state, row, operation, request)
        identifier = request.remote_id if request.resolution == "attach" else operation.remote_id
        if not identifier or (operation.kind != "create" and identifier != operation.remote_id):
            raise ValidationError("Choose the original provider task for reconciliation")
        remote = self.adapter.get_task(connection, actor, identifier, list_id=state.binding.list_id)
        if operation.kind == "create" and (
            task_marker(operation.marker) not in remote.description
            or remote.name != operation.payload["name"]
        ):
            raise ValidationError(
                "Provider task does not contain the exact operation reference and title"
            )
        if operation.kind != "create" and request.resolution == "attach":
            raise ValidationError("Only a task creation supports attaching a provider task")
        with self.store.transaction(actor.household_id):
            current = self.get(actor, project_id)
            self._expected(current, request.expected_version)
            saved = self.store.get_job(operation.id)
            board_job = self._job(actor, project_id)
            assert saved and board_job
            if saved.version != row.version:
                raise InvalidTransitionError("Board operation changed during review")
            # Fence a late request completion and preserve the distinction between a
            # verified task attachment and a user's assessment of an uncertain write.
            resolved = operation.model_copy(
                update={
                    "claim_id": uuid4(),
                    "state": "succeeded" if request.resolution == "attach" else "failed",
                    "remote_id": remote.id,
                    "updated_at": self.clock(),
                    "error": None,
                    "receipt": {
                        "reconciliation": request.resolution,
                        "note": request.note,
                        "provider_task_id": remote.id,
                        "provider_receipt_verified": request.resolution == "attach",
                    },
                }
            )
            self.store.save_job(
                saved.model_copy(
                    update={
                        "result": resolved.model_dump(mode="json"),
                        "status": JobStatus.SUCCEEDED
                        if request.resolution == "attach"
                        else JobStatus.FAILED,
                    }
                ),
                saved.version,
            )
            mappings = {item.todo_id: item for item in current.mappings}
            mapped = self._mapping(operation.todo_id, remote, mappings.get(operation.todo_id))
            if operation.kind == "comment":
                mapped = mapped.model_copy(
                    update={"last_progress_digest": operation.payload.get("progress_hash")}
                )
            if operation.kind == "status":
                mapped = mapped.model_copy(
                    update={
                        "last_published_status": operation.payload.get("status"),
                        "last_status_digest": operation.payload.get("progress_hash"),
                    }
                )
            mappings[operation.todo_id] = mapped
            self.work.record_activity(
                actor,
                project_id,
                ProjectActivityDraft(
                    kind="decision",
                    text="Board operation reviewed: " + request.note,
                    task_id=operation.todo_id,
                ),
                idempotency_key="board-review-" + operation.id.hex,
            )
            return self._save(
                board_job,
                current.model_copy(
                    update={
                        "mappings": tuple(mappings.values()),
                        "pending_operation_ids": tuple(
                            item for item in current.pending_operation_ids if item != operation.id
                        ),
                        "blocked_reasons": (),
                        "next_sync_at": self.clock()
                        + timedelta(seconds=self.sync_interval_seconds),
                    }
                ),
            )

    def _abandon_create(
        self,
        actor: ActorContext,
        project_id: UUID,
        state: ProjectBoardState,
        row: Job,
        operation: BoardOperation,
        request: ReconcileBoardOperation,
    ) -> ProjectBoardState:
        with self.store.transaction(actor.household_id):
            current = self.get(actor, project_id)
            self._expected(current, state.version)
            saved = self.store.get_job(row.id)
            board_job = self._job(actor, project_id)
            assert saved and board_job
            if saved.version != row.version:
                raise InvalidTransitionError("Board operation changed during review")
            resolved = operation.model_copy(
                update={
                    "claim_id": uuid4(),
                    "state": "failed",
                    "updated_at": self.clock(),
                    "error": "Creation abandoned after review; automatic replay is blocked.",
                    "receipt": {
                        "reconciliation": "abandoned",
                        "note": request.note,
                        "provider_receipt_verified": False,
                    },
                }
            )
            self.store.save_job(
                saved.model_copy(
                    update={
                        "result": resolved.model_dump(mode="json"),
                        "status": JobStatus.FAILED,
                    }
                ),
                saved.version,
            )
            self.work.record_activity(
                actor,
                project_id,
                ProjectActivityDraft(
                    kind="decision",
                    text="Board task creation abandoned after review: " + request.note,
                    task_id=operation.todo_id,
                ),
                idempotency_key="board-review-" + operation.id.hex,
            )
            return self._save(
                board_job,
                current.model_copy(
                    update={
                        "abandoned_create_todo_ids": tuple(
                            dict.fromkeys(
                                (
                                    *current.abandoned_create_todo_ids,
                                    operation.todo_id,
                                )
                            )
                        ),
                        "pending_operation_ids": tuple(
                            item for item in current.pending_operation_ids if item != operation.id
                        ),
                        "blocked_reasons": (),
                        "read_failure_count": 0,
                        "next_sync_at": self.clock()
                        + timedelta(seconds=self.sync_interval_seconds),
                    }
                ),
            )
