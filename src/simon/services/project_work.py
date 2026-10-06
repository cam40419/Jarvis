"""Transactional project work state and immutable, owner-scoped activity history."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

from simon.domain.agent_platform import AgentProfile
from simon.domain.errors import (
    AuthorizationError,
    DomainError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Channel, Job, JobStatus, utc_now
from simon.domain.ports import Store
from simon.domain.project_work import (
    ConfigureProjectWork,
    ProjectActivity,
    ProjectActivityDraft,
    ProjectCycle,
    ProjectCycleUpdate,
    ProjectTeam,
    ProjectTodo,
    ProjectWorkControl,
    ProjectWorkState,
)
from simon.services.canonical import digest
from simon.services.identity import ROLE_SCOPES

WORK_KIND = "platform.project_work"
TERMINAL_PHASES = frozenset({"completed", "blocked", "unknown", "cancelled", "waiting"})
BLOCKING_PHASES = frozenset({"blocked", "unknown"})
RUNTIME_TODO_FIELDS = ("cycle_id", "plan_id", "run_id", "run_task_id")


class ProjectWorkService:
    def __init__(
        self,
        store: Store,
        *,
        project_resolver: Callable[[ActorContext, UUID], Any],
        team_validator: Callable[[ActorContext, ProjectTeam], None] | None = None,
        actor_resolver: Callable[[UUID, UUID], ActorContext] | None = None,
        cycle_recovery_validator: Callable[[ActorContext, ProjectCycle], None] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store, self.project_resolver, self.team_validator = (
            store,
            project_resolver,
            team_validator,
        )
        self.actor_resolver, self.clock = actor_resolver or self._resolve_actor, clock
        self.cycle_recovery_validator = cycle_recovery_validator
        self.cycle_retry_validator: Callable[[ActorContext, ProjectCycle], None] | None = None
        self.instruction_resolver: Callable[[ActorContext, UUID], str | None] | None = None
        self.wait_recorder: Callable[[ActorContext, UUID, ProjectCycle], None] | None = None
        self.auto_execution_validator: Callable[[ActorContext, ProjectCycle], bool] | None = None
        self.role_capture: (
            Callable[[ActorContext, str, str, str, tuple[str, ...], int], dict[str, Any]] | None
        ) = None
        self.role_resolver: Callable[[ActorContext, dict[str, Any]], AgentProfile] | None = None
        self.todo_edit_validator: (
            Callable[[ActorContext, UUID, ProjectTodo, ProjectTodo], None] | None
        ) = None

    @staticmethod
    def authorize(actor: ActorContext, *, write: bool = False) -> None:
        scope = "jobs:write" if write else "jobs:read"
        if scope not in actor.scopes:
            raise AuthorizationError("Project work requires " + scope)

    def _resolve_actor(self, actor_id: UUID, workspace_id: UUID) -> ActorContext:
        account = self.store.managed_account(actor_id)
        if account is not None and account.disabled:
            raise AuthorizationError("Account access has been disabled")
        membership = next(
            (
                item
                for item in self.store.memberships(actor_id)
                if item.workspace_id == workspace_id
            ),
            None,
        )
        if membership is None:
            raise AuthorizationError("Project workspace membership required")
        return ActorContext(
            actor_id=actor_id,
            workspace_id=workspace_id,
            channel=Channel.WORKER,
            scopes=ROLE_SCOPES[membership.role],
        )

    def live_actor(self, job: Job) -> ActorContext:
        actor = self.actor_resolver(job.created_by, job.workspace_id)
        if (actor.actor_id, actor.workspace_id) != (job.created_by, job.workspace_id):
            raise AuthorizationError("Project actor resolver returned a different owner")
        actor = actor.model_copy(
            update={
                "scopes": actor.scopes & frozenset(job.input.get("scopes", ())),
            }
        )
        self.authorize(actor, write=True)
        self.project_resolver(actor, UUID(job.input["project_id"]))
        return actor

    @staticmethod
    def identifier(actor: ActorContext, project_id: UUID) -> UUID:
        return uuid5(project_id, f"project-work:{actor.workspace_id}:{actor.actor_id}")

    @staticmethod
    def activity_kind(project_id: UUID) -> str:
        return "platform.project_activity." + project_id.hex

    def _job(self, actor: ActorContext, project_id: UUID, *, create: bool = False) -> Job | None:
        self.project_resolver(actor, project_id)
        identifier = self.identifier(actor, project_id)
        job = self.store.get_job(identifier)
        if job is not None and (
            job.kind != WORK_KIND
            or job.created_by != actor.actor_id
            or job.workspace_id != actor.workspace_id
        ):
            raise NotFoundError("Project work not found")
        if job is None and create:
            state = ProjectWorkState(
                project_id=project_id,
                workspace_id=actor.workspace_id,
                actor_id=actor.actor_id,
                updated_at=self.clock(),
            )
            job, _ = self.store.create_job(
                Job(
                    id=identifier,
                    workspace_id=actor.workspace_id,
                    created_by=actor.actor_id,
                    kind=WORK_KIND,
                    idempotency_key=identifier.hex,
                    status=JobStatus.WAITING,
                    input={
                        "project_id": str(project_id),
                        "scopes": sorted(actor.scopes),
                        "rank": 0,
                        "initial_state": state.model_dump(mode="json"),
                    },
                    input_digest=digest({"project_id": str(project_id)}),
                    result=state.model_dump(mode="json"),
                )
            )
        return job

    @staticmethod
    def view(job: Job) -> ProjectWorkState:
        return ProjectWorkState.model_validate(job.result or job.input["initial_state"]).model_copy(
            update={"version": job.version},
        )

    def get(self, actor: ActorContext, project_id: UUID) -> ProjectWorkState:
        self.authorize(actor)
        job = self._job(actor, project_id)
        return (
            self.view(job)
            if job
            else ProjectWorkState(
                project_id=project_id,
                workspace_id=actor.workspace_id,
                actor_id=actor.actor_id,
                updated_at=self.clock(),
            )
        )

    def _save(self, job: Job, state: ProjectWorkState) -> ProjectWorkState:
        state = ProjectWorkState.model_validate(state.model_dump(mode="python")).model_copy(
            update={"updated_at": self.clock()},
        )
        status = JobStatus.WAITING
        if state.active_cycle:
            status = JobStatus.RUNNING
        elif state.blocked_reasons:
            status = JobStatus.NEEDS_HUMAN
        elif state.autonomy.mode == "scheduled" and not state.autonomy.paused:
            status = JobStatus.QUEUED
        updated = job.model_copy(
            update={
                "input": {
                    **job.input,
                    "rank": int(state.next_cycle_at.timestamp()) if state.next_cycle_at else 0,
                },
                "result": state.model_dump(mode="json"),
                "status": status,
                "updated_at": self.clock(),
            }
        )
        return self.view(self.store.save_job(updated, job.version))

    def _member_records(
        self,
        actor: ActorContext,
        project_id: UUID,
    ) -> dict[str, tuple[AgentProfile | None, tuple[str, ...]]]:
        self.authorize(actor)
        job = self._job(actor, project_id)
        if job is None:
            return {}
        state = self.view(job)
        if state.team is None:
            return {}
        snapshots = job.input.get("member_snapshots", {})
        records: dict[str, tuple[AgentProfile | None, tuple[str, ...]]] = {}
        for identifier in state.team.members:
            try:
                snapshot = snapshots.get(identifier) if isinstance(snapshots, dict) else None
                if (
                    not isinstance(snapshot, dict)
                    or snapshot.get("project_id") != str(project_id)
                    or not isinstance(snapshot.get("role"), dict)
                ):
                    raise ValidationError("Saved project member permissions are unavailable")
                if self.role_resolver is None:
                    raise ValidationError("Project member resolution is not configured")
                if snapshot["role"].get("definition") != state.team.members[identifier].model_dump(
                    mode="json",
                ):
                    raise ValidationError("Saved project member role does not match its definition")
                profile = self.role_resolver(actor, snapshot["role"])
                if profile.id != identifier:
                    raise ValidationError("Saved project member identity does not match")
                records[identifier] = (profile, ())
            except DomainError as error:
                records[identifier] = (None, (str(error),))
        return records

    def member_profiles(
        self,
        actor: ActorContext,
        project_id: UUID,
    ) -> dict[str, AgentProfile | None]:
        # Keep blocked IDs in the mapping: they must never fall back to a stock
        # or globally saved profile with broader permissions under the same ID.
        return {
            identifier: row[0]
            for identifier, row in self._member_records(actor, project_id).items()
        }

    def member_profile_statuses(
        self,
        actor: ActorContext,
        project_id: UUID,
    ) -> list[dict[str, Any]]:
        return [
            {
                "agent_id": identifier,
                "state": "configured" if profile else "blocked",
                "profile": profile.model_dump(mode="json") if profile else None,
                "blocked_reasons": list(reasons),
            }
            for identifier, (profile, reasons) in self._member_records(actor, project_id).items()
        ]

    def _capture_members(
        self,
        actor: ActorContext,
        project_id: UUID,
        old: ProjectTeam | None,
        team: ProjectTeam,
        saved: Any,
    ) -> dict[str, Any]:
        if not isinstance(saved, dict):
            raise ValidationError("Saved project member permissions are invalid")
        snapshots = {}
        for identifier, definition in team.members.items():
            if old is not None and old.members.get(identifier) == definition:
                if identifier not in saved:
                    raise ValidationError("Saved project member permissions are unavailable")
                snapshots[identifier] = saved[identifier]
                continue
            if self.role_capture is None:
                raise ValidationError("Project member configuration is not installed")
            snapshots[identifier] = {
                "project_id": str(project_id),
                "role": self.role_capture(
                    actor,
                    identifier,
                    definition.name,
                    definition.description,
                    definition.skill_ids,
                    team.revision,
                ),
            }
        return snapshots

    @staticmethod
    def _expected(state: ProjectWorkState, expected: int) -> None:
        if state.version != expected:
            raise InvalidTransitionError("Project work changed; refresh before editing")

    def _activity(
        self,
        actor: ActorContext,
        state: ProjectWorkState,
        draft: ProjectActivityDraft,
        key: str,
    ) -> ProjectWorkState:
        identifier = uuid5(self.identifier(actor, state.project_id), "activity:" + key)
        old = self.store.get_job(identifier)
        payload = draft.model_dump(mode="json")
        if old:
            if old.input_digest != digest(payload):
                raise IdempotencyConflictError("Activity key already belongs to different content")
            return state
        activity = ProjectActivity(
            **payload,
            id=identifier,
            project_id=state.project_id,
            sequence=state.activity_count + 1,
            created_at=self.clock(),
        )
        self.store.create_job(
            Job(
                id=identifier,
                workspace_id=actor.workspace_id,
                created_by=actor.actor_id,
                kind=self.activity_kind(state.project_id),
                idempotency_key=identifier.hex,
                input={
                    "rank": -activity.sequence,
                    "project_id": str(state.project_id),
                    "initial_state": activity.model_dump(mode="json"),
                },
                input_digest=digest(payload),
                status=JobStatus.SUCCEEDED,
                result=activity.model_dump(mode="json"),
            )
        )
        return state.model_copy(update={"activity_count": activity.sequence})

    def configure(
        self,
        actor: ActorContext,
        project_id: UUID,
        body: ConfigureProjectWork,
    ) -> ProjectWorkState:
        self.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            current = self.get(actor, project_id)
            self._expected(current, body.expected_version)
            existing_job = self._job(actor, project_id)
            team = current.team
            if body.team is not None:
                if self.team_validator is None:
                    raise ValidationError("Project team validation is not configured")
                proposed = body.team
                if team is not None and "members" not in body.team.model_fields_set:
                    proposed = proposed.model_copy(
                        update={
                            "members": {
                                key: value
                                for key, value in team.members.items()
                                if key in proposed.agent_ids
                            },
                        }
                    )
                changed = team is None or (
                    team.model_dump(exclude={"revision"})
                    != proposed.model_dump(exclude={"revision"})
                )
                if changed and current.active_cycle:
                    raise InvalidTransitionError(
                        "Finish or discard the active cycle before changing teams"
                    )
                self.team_validator(actor, proposed)
                if changed:
                    team = proposed.model_copy(
                        update={"revision": team.revision + 1 if team else 1}
                    )
            autonomy = body.autonomy or current.autonomy
            if (
                current.active_cycle
                and body.autonomy
                and (
                    current.autonomy.model_dump(exclude={"paused"})
                    != autonomy.model_dump(exclude={"paused"})
                )
            ):
                raise InvalidTransitionError(
                    "Finish or discard the active cycle before changing its limits"
                )
            if autonomy.mode == "scheduled" and team is None:
                raise ValidationError("Select a project team before scheduling work")
            used = current.scheduled_cycles_used
            if current.autonomy.mode != "scheduled" and autonomy.mode == "scheduled":
                used = 0
            next_at = current.next_cycle_at
            if autonomy.mode == "scheduled" and next_at is None:
                next_at = self.clock() + timedelta(minutes=autonomy.cadence_minutes)
            elif autonomy.mode == "manual":
                next_at = None
            state = current.model_copy(
                update={
                    "team": team,
                    "autonomy": autonomy,
                    "scheduled_cycles_used": used,
                    "next_cycle_at": next_at,
                }
            )
            if current.active_cycle and current.autonomy.paused != autonomy.paused:
                state = state.model_copy(
                    update={
                        "active_cycle": current.active_cycle.model_copy(
                            update={
                                "revision": current.active_cycle.revision + 1,
                                "updated_at": self.clock(),
                            },
                        )
                    }
                )
            job = self._job(actor, project_id, create=True)
            assert job is not None
            if body.team is not None and team is not None:
                snapshots = self._capture_members(
                    actor,
                    project_id,
                    current.team,
                    team,
                    existing_job.input.get("member_snapshots", {}) if existing_job else {},
                )
                job = job.model_copy(update={"input": {**job.input, "member_snapshots": snapshots}})
            state = self._activity(
                actor,
                state,
                ProjectActivityDraft(
                    kind="configuration",
                    text="Project team or work preferences updated.",
                ),
                f"configuration:{current.version}",
            )
            return self._save(job, state)

    def list_activity(
        self,
        actor: ActorContext,
        project_id: UUID,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.authorize(actor)
        self.project_resolver(actor, project_id)
        if not 0 <= offset <= 10_000_000 or not 1 <= limit <= 100:
            raise ValidationError("Invalid project activity page")
        rows = self.store.jobs(
            actor.workspace_id, actor.actor_id, self.activity_kind(project_id), offset, limit + 1
        )
        return {
            "items": [
                ProjectActivity.model_validate(
                    job.result or job.input["initial_state"],
                ).model_dump(mode="json")
                for job in rows[:limit]
            ],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def record_activity(
        self,
        actor: ActorContext,
        project_id: UUID,
        draft: ProjectActivityDraft,
        *,
        idempotency_key: str,
    ) -> ProjectWorkState:
        self.authorize(actor, write=True)
        self._key(idempotency_key)
        with self.store.transaction(actor.workspace_id):
            job = self._job(actor, project_id, create=True)
            assert job is not None
            current = self.view(job)
            state = self._activity(actor, current, draft, "record:" + idempotency_key)
            return self._save(job, state) if state != current else current

    @staticmethod
    def _key(value: str) -> None:
        if not isinstance(value, str) or not 8 <= len(value) <= 180:
            raise ValidationError("Use an idempotency key between 8 and 180 characters")

    @staticmethod
    def _todos(
        state: ProjectWorkState, updates: tuple[ProjectTodo, ...]
    ) -> tuple[ProjectTodo, ...]:
        rows = {todo.id: todo for todo in state.todos}
        if len({todo.id for todo in updates}) != len(updates):
            raise ValidationError("Duplicate project task IDs")
        for todo in updates:
            if todo.agent_id and (state.team is None or todo.agent_id not in state.team.agent_ids):
                raise ValidationError("Task assignee must belong to the project team")
            rows[todo.id] = todo
        if len(rows) > 500:
            raise ValidationError(
                "Project backlog is full; archive completed tasks before adding work"
            )
        graph = {identifier: set(todo.depends_on) for identifier, todo in rows.items()}
        if any(not deps <= rows.keys() for deps in graph.values()):
            raise ValidationError("Project task dependency was not found")
        while graph:
            ready = {identifier for identifier, deps in graph.items() if not deps}
            if not ready:
                raise ValidationError("Project task dependencies must not contain cycles")
            graph = {
                identifier: deps - ready
                for identifier, deps in graph.items()
                if identifier not in ready
            }
        return tuple(rows.values())

    def add_todo(
        self,
        actor: ActorContext,
        project_id: UUID,
        todo: ProjectTodo,
        *,
        idempotency_key: str,
    ) -> ProjectWorkState:
        self.authorize(actor, write=True)
        self._key(idempotency_key)
        if any(getattr(todo, name) is not None for name in RUNTIME_TODO_FIELDS):
            raise ValidationError("Task execution links are assigned by the project coordinator")
        if todo.status in {"running", "unknown", "archived"}:
            raise ValidationError("New tasks cannot claim a worker outcome or archived state")
        with self.store.transaction(actor.workspace_id):
            payload = todo.model_dump(mode="json", exclude={"created_at", "updated_at"})
            if "id" not in todo.model_fields_set:
                payload.pop("id")

            def operation() -> dict[str, Any]:
                job = self._job(actor, project_id, create=True)
                assert job is not None
                current = self.view(job)
                selected = (
                    todo
                    if "id" in todo.model_fields_set
                    else todo.model_copy(
                        update={
                            "id": "todo-" + uuid5(job.id, "todo:" + idempotency_key).hex,
                        }
                    )
                )
                if any(item.id == selected.id for item in current.todos):
                    raise InvalidTransitionError(
                        "Task exists; use its current version to update it"
                    )
                archived = uuid5(job.id, "archived-todo:" + selected.id)
                if self.store.get_job(archived):
                    raise InvalidTransitionError("An archived task already uses this ID")
                state = current.model_copy(update={"todos": self._todos(current, (selected,))})
                state = self._activity(
                    actor,
                    state,
                    ProjectActivityDraft(
                        kind="task",
                        text="Task added: " + selected.title,
                        task_id=selected.id,
                    ),
                    "add-todo:" + idempotency_key,
                )
                self._save(job, state)
                return {"id": selected.id}

            self.store.execute_once(
                f"project-todo:{actor.workspace_id}:{actor.actor_id}:{project_id}",
                idempotency_key,
                digest(payload),
                operation,
            )
            return self.get(actor, project_id)

    def update_todo(
        self,
        actor: ActorContext,
        project_id: UUID,
        todo: ProjectTodo,
        *,
        expected_version: int,
    ) -> ProjectWorkState:
        self.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            job = self._job(actor, project_id)
            if job is None:
                raise NotFoundError("Project task not found")
            state = self.view(job)
            self._expected(state, expected_version)
            old = next((item for item in state.todos if item.id == todo.id), None)
            if old is None:
                raise NotFoundError("Project task not found")
            if self.todo_edit_validator is not None:
                self.todo_edit_validator(actor, project_id, old, todo)
            if any(getattr(todo, name) != getattr(old, name) for name in RUNTIME_TODO_FIELDS):
                raise ValidationError("Saved task execution links cannot be edited")
            if todo.status in {"running", "unknown"} and todo.status != old.status:
                raise ValidationError("Only the worker can assign an execution outcome")
            if old.status == "running" or (
                state.active_cycle and old.cycle_id == state.active_cycle.id
            ):
                raise InvalidTransitionError("Active task progress is owned by its project cycle")
            todo = todo.model_copy(
                update={"created_at": old.created_at, "updated_at": self.clock()}
            )
            if todo.status == "archived":
                if old.status not in {"done", "cancelled"}:
                    raise InvalidTransitionError("Only finished or cancelled tasks can be archived")
                if any(todo.id in other.depends_on for other in state.todos):
                    raise InvalidTransitionError(
                        "Archive dependent tasks before their prerequisites"
                    )
                identifier = uuid5(job.id, "archived-todo:" + todo.id)
                self.store.create_job(
                    Job(
                        id=identifier,
                        workspace_id=actor.workspace_id,
                        created_by=actor.actor_id,
                        kind="platform.project_archive." + project_id.hex,
                        idempotency_key=identifier.hex,
                        status=JobStatus.SUCCEEDED,
                        input={
                            "initial_state": todo.model_dump(mode="json"),
                            "rank": int(self.clock().timestamp()),
                        },
                        input_digest=digest(todo.model_dump(mode="json")),
                    )
                )
                state = state.model_copy(
                    update={
                        "todos": tuple(item for item in state.todos if item.id != todo.id),
                    }
                )
            else:
                state = state.model_copy(update={"todos": self._todos(state, (todo,))})
            state = self._activity(
                actor,
                state,
                ProjectActivityDraft(
                    kind="task",
                    text="Task updated: " + todo.title,
                    task_id=todo.id,
                ),
                f"task-update:{expected_version}",
            )
            return self._save(job, state)

    @staticmethod
    def is_continuation(instruction: str) -> bool:
        return instruction.strip().lower().strip(".!? ") in {
            "continue",
            "proceed",
            "resume",
            "retry",
            "try again",
            "please continue",
            "please proceed",
            "continue please",
        }

    def continuation_instruction(self, actor: ActorContext, state: ProjectWorkState) -> str | None:
        self.authorize(actor)
        self.project_resolver(actor, state.project_id)
        for instruction in (
            state.last_instruction,
            state.last_cycle.instruction if state.last_cycle else None,
        ):
            if instruction and not self.is_continuation(instruction):
                return instruction
        return (
            self.instruction_resolver(actor, state.project_id)
            if self.instruction_resolver is not None
            else None
        )

    def request_cycle(
        self,
        actor: ActorContext,
        project_id: UUID,
        instruction: str,
        idempotency_key: str,
        *,
        automatic: bool = False,
        expected_version: int | None = None,
        replace_failed: bool = False,
        target_agent_id: str | None = None,
        request_id: UUID | None = None,
        bounded_execution: bool = False,
        model_budget_usd: float | None = None,
    ) -> ProjectWorkState:
        self.authorize(actor, write=True)
        self._key(idempotency_key)
        if not instruction.strip():
            raise ValidationError("A project instruction is required")
        if replace_failed and (automatic or expected_version is None):
            raise ValidationError("Replacing failed planning requires a current manual request")
        with self.store.transaction(actor.workspace_id):

            def create() -> dict[str, Any]:
                job = self._job(actor, project_id, create=True)
                assert job is not None
                state = self.view(job)
                if expected_version is not None:
                    self._expected(state, expected_version)
                    if automatic and (
                        state.next_cycle_at is None
                        or state.next_cycle_at > self.clock()
                        or instruction != state.autonomy.objective
                    ):
                        raise InvalidTransitionError("Scheduled work is not due for this objective")
                if state.team is None:
                    raise ValidationError("Select a project team before asking its lead to work")
                if self.team_validator:
                    self.team_validator(actor, state.team)
                if target_agent_id is not None and target_agent_id not in state.team.agent_ids:
                    raise ValidationError("The selected agent must belong to this project team")
                if replace_failed:
                    previous = state.last_cycle
                    if (
                        state.active_cycle is not None
                        or previous is None
                        or previous.phase != "blocked"
                        or previous.execution_run_id is not None
                        or not state.blocked_reasons
                        or self.cycle_retry_validator is None
                    ):
                        raise InvalidTransitionError(
                            "Only settled failed planning can be replaced with a new request"
                        )
                    self.cycle_retry_validator(actor, previous)
                    state = state.model_copy(
                        update={
                            "blocked_reasons": (),
                            "autonomy": state.autonomy.model_copy(update={"paused": False}),
                        }
                    )
                    state = self._activity(
                        actor,
                        state,
                        ProjectActivityDraft(
                            kind="decision",
                            text="Replaced failed planning with a new request; the previous "
                            "attempt remains in history.",
                        ),
                        "replace-request:" + idempotency_key,
                    )
                if state.active_cycle or state.autonomy.paused:
                    raise InvalidTransitionError("Project is paused or already has an active cycle")
                if state.blocked_reasons:
                    raise InvalidTransitionError(
                        "Review and acknowledge the previous blocked outcome first"
                    )
                if automatic and (
                    state.autonomy.mode != "scheduled"
                    or state.scheduled_cycles_used >= state.autonomy.max_cycles
                    or state.autonomy.model_budget_usd is None
                ):
                    raise InvalidTransitionError(
                        "Scheduled project work has reached its configured limits"
                    )
                resolved_instruction = instruction
                if not automatic and self.is_continuation(instruction):
                    resolved_instruction = self.continuation_instruction(actor, state) or ""
                    if not resolved_instruction:
                        raise ValidationError(
                            "Describe the work to continue; no previous request is saved"
                        )
                cycle = ProjectCycle(
                    number=state.cycle_count + 1,
                    instruction=resolved_instruction,
                    automatic=automatic,
                    model_budget_usd=model_budget_usd
                    if model_budget_usd is not None
                    else state.autonomy.model_budget_usd,
                    bounded_execution=bounded_execution
                    or state.autonomy.execution_policy == "bounded",
                    target_agent_id=target_agent_id,
                    request_id=request_id,
                    started_at=self.clock(),
                    updated_at=self.clock(),
                )
                if cycle.bounded_execution and cycle.model_budget_usd is None:
                    raise ValidationError("Bounded execution requires a finite model budget")
                state = state.model_copy(
                    update={
                        "active_cycle": cycle,
                        "cycle_count": cycle.number,
                        "scheduled_cycles_used": state.scheduled_cycles_used + int(automatic),
                        "last_instruction": resolved_instruction,
                    }
                )
                state = self._activity(
                    actor,
                    state,
                    ProjectActivityDraft(
                        kind="cycle",
                        text="Project lead cycle queued: " + instruction[:1000],
                    ),
                    "cycle-request:" + idempotency_key,
                )
                if not automatic:
                    job = job.model_copy(
                        update={"input": {**job.input, "scopes": sorted(actor.scopes)}}
                    )
                saved = self._save(job, state)
                return {"cycle_id": str(cycle.id), "version": saved.version}

            self.store.execute_once(
                f"project-cycle:{actor.workspace_id}:{actor.actor_id}:{project_id}",
                idempotency_key,
                digest(
                    {
                        "instruction": instruction,
                        "automatic": automatic,
                        **({"replace_failed": True} if replace_failed else {}),
                        **({"target_agent_id": target_agent_id} if target_agent_id else {}),
                        **({"request_id": str(request_id)} if request_id else {}),
                        **({"bounded_execution": True} if bounded_execution else {}),
                        **({"model_budget_usd": model_budget_usd} if model_budget_usd else {}),
                    }
                ),
                create,
            )
            return self.get(actor, project_id)

    def control(
        self,
        actor: ActorContext,
        project_id: UUID,
        body: ProjectWorkControl,
    ) -> ProjectWorkState:
        self.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            job = self._job(actor, project_id)
            if job is None:
                raise NotFoundError("Project work not found")
            state = self.view(job)
            self._expected(state, body.expected_version)
            cycle = state.active_cycle
            if body.action == "retry":
                previous = state.last_cycle
                if (
                    cycle is not None
                    or previous is None
                    or previous.phase != "blocked"
                    or previous.execution_run_id is not None
                    or not state.blocked_reasons
                ):
                    raise InvalidTransitionError(
                        "Retry is available only for blocked planning before delegated work starts"
                    )
                if self.cycle_retry_validator is None:
                    raise InvalidTransitionError("Planning retry validation is unavailable")
                self.cycle_retry_validator(actor, previous)
                if state.team is None:
                    raise ValidationError("Select a project team before retrying")
                if self.team_validator is not None:
                    self.team_validator(actor, state.team)
                instruction = self.continuation_instruction(actor, state)
                if instruction is None:
                    raise ValidationError("Describe the original request before retrying")
                cycle = ProjectCycle(
                    number=state.cycle_count + 1,
                    instruction=instruction,
                    model_budget_usd=state.autonomy.model_budget_usd,
                    started_at=self.clock(),
                    updated_at=self.clock(),
                )
                state = state.model_copy(
                    update={
                        "active_cycle": cycle,
                        "cycle_count": cycle.number,
                        "last_instruction": instruction,
                        "blocked_reasons": (),
                        "next_cycle_at": None,
                        "todos": tuple(
                            todo.model_copy(
                                update={
                                    "status": "ready",
                                    "error": None,
                                    "updated_at": self.clock(),
                                }
                            )
                            if todo.cycle_id == previous.id and todo.status in {"ready", "blocked"}
                            else todo
                            for todo in state.todos
                        ),
                        "autonomy": state.autonomy.model_copy(
                            update={"paused": False, "mode": "manual"}
                        ),
                    }
                )
                job = job.model_copy(
                    update={"input": {**job.input, "scopes": sorted(actor.scopes)}}
                )
            elif body.action in {"pause", "resume"}:
                paused = body.action == "pause"
                if not paused and state.blocked_reasons:
                    raise InvalidTransitionError("Acknowledge blocked outcomes before resuming")
                state = state.model_copy(
                    update={
                        "autonomy": state.autonomy.model_copy(update={"paused": paused}),
                    }
                )
                if cycle:
                    state = state.model_copy(
                        update={
                            "active_cycle": cycle.model_copy(
                                update={
                                    "revision": cycle.revision + 1,
                                    "updated_at": self.clock(),
                                }
                            )
                        }
                    )
            elif body.action == "run_ready":
                if state.autonomy.paused or not cycle or cycle.phase != "ready":
                    raise InvalidTransitionError("An unpaused, reviewed project plan is required")
                state = state.model_copy(
                    update={
                        "active_cycle": cycle.model_copy(
                            update={
                                "execution_approved": True,
                                "revision": cycle.revision + 1,
                                "updated_at": self.clock(),
                            }
                        )
                    }
                )
            elif body.action == "discard":
                if cycle is None:
                    raise InvalidTransitionError("There is no active cycle to discard")
                if cycle.phase not in {"starting", "ready"} or (
                    cycle.phase == "starting" and cycle.planning_run_id is not None
                ):
                    raise InvalidTransitionError(
                        "Pause and drain active agent runs before discarding"
                    )
                cycle = cycle.model_copy(
                    update={
                        "phase": "cancelled",
                        "finished_at": self.clock(),
                        "revision": cycle.revision + 1,
                        "updated_at": self.clock(),
                    }
                )
                state = state.model_copy(
                    update={
                        "active_cycle": None,
                        "last_cycle": cycle,
                        "todos": tuple(
                            todo.model_copy(
                                update={"status": "cancelled", "updated_at": self.clock()}
                            )
                            if todo.cycle_id == cycle.id
                            else todo
                            for todo in state.todos
                        ),
                        "next_cycle_at": self.clock()
                        + timedelta(
                            minutes=state.autonomy.cadence_minutes,
                        )
                        if state.autonomy.mode == "scheduled"
                        else None,
                    }
                )
            else:
                if cycle or not state.blocked_reasons or not body.note.strip():
                    raise InvalidTransitionError(
                        "A review note and a settled blocked cycle are required"
                    )
                previous = state.last_cycle
                if previous and (previous.planning_run_id or previous.execution_run_id):
                    if self.cycle_recovery_validator is None:
                        raise InvalidTransitionError("Linked runs require operator recovery review")
                    self.cycle_recovery_validator(actor, previous)
                state = state.model_copy(update={"blocked_reasons": ()})
            state = self._activity(
                actor,
                state,
                ProjectActivityDraft(
                    kind="decision",
                    text="Project control: "
                    + body.action
                    + (". " + body.note if body.note else ""),
                ),
                f"control:{body.expected_version}",
            )
            return self._save(job, state)

    def _apply(self, actor: ActorContext, job: Job, update: ProjectCycleUpdate) -> ProjectWorkState:
        state = self.view(job)
        current, cycle = state.active_cycle, update.cycle
        if current is None or cycle.id != current.id or cycle.revision != current.revision:
            raise InvalidTransitionError("Project cycle changed; stale coordinator result rejected")
        immutable = {
            "id",
            "number",
            "instruction",
            "automatic",
            "model_budget_usd",
            "started_at",
            "bounded_execution",
            "target_agent_id",
            "request_id",
            "parent_cycle_id",
        }
        if any(getattr(cycle, name) != getattr(current, name) for name in immutable):
            raise ValidationError("Coordinator cannot change the cycle's original authorization")
        allowed = {
            "starting": {"starting", "planning", "blocked", "unknown", "cancelled"},
            "planning": {
                "planning",
                "ready",
                "completed",
                "blocked",
                "unknown",
                "cancelled",
                "waiting",
            },
            "ready": {"ready", "executing", "blocked", "unknown", "cancelled"},
            "executing": {"executing", "completed", "blocked", "unknown", "cancelled"},
        }
        if cycle.phase not in allowed.get(current.phase, set()):
            raise InvalidTransitionError("Project cycle cannot move to that phase")
        for name in (
            "planning_plan_id",
            "planning_run_id",
            "execution_plan_id",
            "execution_run_id",
        ):
            if getattr(current, name) is not None and getattr(cycle, name) != getattr(
                current, name
            ):
                raise ValidationError("A linked plan or run cannot be replaced")
        if cycle.phase == "planning" and not (cycle.planning_plan_id and cycle.planning_run_id):
            raise ValidationError("Planning requires durable plan and run links")
        if cycle.phase == "ready" and not cycle.execution_plan_id:
            raise ValidationError("Ready work requires a durable execution plan")
        if cycle.phase == "executing":
            if not (cycle.execution_plan_id and cycle.execution_run_id):
                raise ValidationError("Executing work requires durable plan and run links")
            if not (current.automatic or current.execution_approved):
                raise AuthorizationError("Manual delegated work requires plan approval")
        if current.execution_approved and not cycle.execution_approved:
            raise ValidationError("Coordinator cannot remove saved execution approval")
        if (
            cycle.execution_approved
            and not current.execution_approved
            and not current.automatic
            and not (
                current.bounded_execution
                and self.auto_execution_validator is not None
                and self.auto_execution_validator(actor, cycle)
            )
        ):
            raise AuthorizationError("Manual delegated work requires the user's plan approval")
        if cycle.model_reserved_usd < current.model_reserved_usd:
            raise ValidationError("Reserved model budget cannot be released or forgotten")
        if cycle.model_budget_usd is not None and cycle.model_reserved_usd > cycle.model_budget_usd:
            raise ValidationError("Project cycle model budget exceeded")
        if state.autonomy.paused and (
            cycle.planning_run_id != current.planning_run_id
            or cycle.execution_run_id != current.execution_run_id
        ):
            raise InvalidTransitionError("Paused projects cannot enqueue new work")
        if cycle == current and not update.todos and not update.activity:
            return state
        cycle = cycle.model_copy(
            update={"revision": current.revision + 1, "updated_at": self.clock()}
        )
        updates = {"active_cycle": cycle, "todos": self._todos(state, update.todos)}
        if cycle.phase in TERMINAL_PHASES:
            cycle = cycle.model_copy(update={"finished_at": self.clock()})
            updates.update(active_cycle=None, last_cycle=cycle)
            if cycle.phase == "waiting":
                if self.wait_recorder is None:
                    raise ValidationError("Durable project waits are unavailable")
                self.wait_recorder(actor, state.project_id, cycle)
            if cycle.phase in BLOCKING_PHASES:
                updates["blocked_reasons"] = (
                    cycle.error or "Review the interrupted project cycle.",
                )
                updates["autonomy"] = state.autonomy.model_copy(update={"paused": True})
            if state.autonomy.mode == "scheduled":
                updates["next_cycle_at"] = self.clock() + timedelta(
                    minutes=state.autonomy.cadence_minutes,
                )
        state = state.model_copy(update=updates)
        for index, activity in enumerate(update.activity):
            state = self._activity(
                actor, state, activity, f"cycle:{current.id}:{current.revision}:{index}"
            )
        return self._save(job, state)

    def apply_cycle(
        self,
        actor: ActorContext,
        project_id: UUID,
        update: ProjectCycleUpdate,
    ) -> ProjectWorkState:
        self.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            job = self._job(actor, project_id)
            if job is None:
                raise NotFoundError("Project work not found")
            return self._apply(actor, job, update)

    def update_cycle_atomic(
        self,
        actor: ActorContext,
        project_id: UUID,
        cycle_id: UUID,
        operation: Callable[[ProjectWorkState], ProjectCycleUpdate],
    ) -> ProjectWorkState:
        """Atomically enqueue a DB-backed plan/run and save its link; never do network I/O here."""
        self.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            job = self._job(actor, project_id)
            if job is None:
                raise NotFoundError("Project work not found")
            state = self.view(job)
            if (
                state.autonomy.paused
                or state.active_cycle is None
                or state.active_cycle.id != cycle_id
            ):
                raise InvalidTransitionError("Project is paused or its active cycle changed")
            return self._apply(actor, job, operation(state))

    def halt(self, candidate: Job, reason: str, *, uncertain: bool = False) -> ProjectWorkState:
        """Internal fail-closed recovery; never starts, retries or cancels a provider operation."""
        with self.store.transaction(candidate.workspace_id):
            job = self.store.get_job(candidate.id)
            if job is None or job.kind != WORK_KIND:
                raise NotFoundError("Project work not found")
            state, previous = self.view(job), self.view(candidate)
            if (
                state.active_cycle
                and previous.active_cycle
                and state.active_cycle.id != previous.active_cycle.id
            ):
                return state
            if previous.active_cycle is None and job.version != candidate.version:
                return state
            if previous.active_cycle is not None and state.active_cycle is None:
                return state
            cycle = state.active_cycle
            changes: dict[str, Any] = {
                "autonomy": state.autonomy.model_copy(update={"paused": True}),
                "blocked_reasons": (reason[:2000],),
            }
            if cycle:
                phase = (
                    "unknown"
                    if (uncertain or cycle.planning_run_id or cycle.execution_run_id)
                    else "blocked"
                )
                changes.update(
                    active_cycle=None,
                    last_cycle=cycle.model_copy(
                        update={
                            "phase": phase,
                            "error": reason[:2000],
                            "finished_at": self.clock(),
                            "updated_at": self.clock(),
                            "revision": cycle.revision + 1,
                        }
                    ),
                )
            state = state.model_copy(update=changes)
            actor = ActorContext(
                actor_id=job.created_by, workspace_id=job.workspace_id, channel=Channel.WORKER
            )
            state = self._activity(
                actor,
                state,
                ProjectActivityDraft(kind="blocked", text=reason),
                f"halt:{job.version}",
            )
            return self._save(job, state)

    def list_archived(
        self,
        actor: ActorContext,
        project_id: UUID,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        self.authorize(actor)
        self.project_resolver(actor, project_id)
        if not 0 <= offset <= 10_000_000 or not 1 <= limit <= 100:
            raise ValidationError("Invalid archived task page")
        rows = self.store.jobs(
            actor.workspace_id,
            actor.actor_id,
            "platform.project_archive." + project_id.hex,
            offset,
            limit + 1,
        )
        return {
            "items": [
                ProjectTodo.model_validate(job.input["initial_state"]).model_dump(mode="json")
                for job in rows[:limit]
            ],
            "next_offset": offset + limit if len(rows) > limit else None,
        }
