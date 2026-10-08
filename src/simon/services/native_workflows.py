"""Human grants, task definitions and durable native workflow admission."""

import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from uuid import UUID, uuid4, uuid5

from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_execution import (
    CreateExecutionSchedule,
    EnrollExecutionRunner,
    NativeExecutionEvent,
    NativeExecutionPolicy,
    NativeExecutionRun,
    NativeExecutionRunner,
    NativeExecutionSchedule,
    NativeTaskWorkflow,
    StartExecution,
    UpdateExecutionPolicy,
    UpdateExecutionSchedule,
    UpdateTaskWorkflow,
    bounded_json,
)
from simon.domain.native_projects import (
    NativeCommand,
    NativeModel,
    NativeTask,
    TaskAssignment,
    UpdateNativeTask,
    VersionedNativeCommand,
)
from simon.domain.ports import Store
from simon.services.canonical import digest
from simon.services.identity import IDENTITY_LOCK, ROLE_SCOPES
from simon.services.native_projects import NativeProjectService
from simon.services.project_models import ProjectModelService

LIVE = frozenset({"queued", "running", "waiting", "unknown"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "stale"})
_Record = TypeVar("_Record", bound=NativeModel)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def task_digest(task: NativeTask) -> str:
    return digest(
        task.model_dump(mode="json", include={"id", "title", "description", "assignment"})
    )


def public_run(run: NativeExecutionRun) -> dict[str, Any]:
    return {
        **run.model_dump(mode="json", exclude={"lease_token_hash", "context"}),
        "task_title": run.context.get("task", {}).get("title", ""),
        "agent_name": run.context.get("role", {}).get("name", ""),
    }


def public_runner(runner: NativeExecutionRunner) -> dict[str, Any]:
    return runner.model_dump(mode="json", exclude={"token_hash"})


class NativeWorkflowService:
    def __init__(
        self, store: Store, projects: NativeProjectService, models: ProjectModelService
    ) -> None:
        self.store, self.projects, self.models, self.usage = store, projects, models, models.usage

    def _access(self, actor: ActorContext, project_id: UUID, *, write: bool = False) -> None:
        if not isinstance(actor, ActorContext) or actor.channel != Channel.API:
            raise AuthorizationError("Execution controls require a human project session.")
        project = self.projects._project(actor, project_id, write=write, owner=write)
        if write:
            self.projects._active(project)

    def _issuer(self, workspace_id: UUID, project_id: UUID, actor_id: UUID) -> ActorContext:
        member = self.projects._membership(workspace_id, actor_id)
        if member is None:
            raise AuthorizationError("The execution grant owner no longer has access.")
        actor = ActorContext(
            actor_id=actor_id,
            workspace_id=workspace_id,
            scopes=ROLE_SCOPES[member.role],
            channel=Channel.API,
        )
        self._access(actor, project_id, write=True)
        return actor

    def _policy(self, workspace_id: UUID, project_id: UUID) -> NativeExecutionPolicy:
        return self.store.execution_policy(workspace_id, project_id) or NativeExecutionPolicy(
            workspace_id=workspace_id, project_id=project_id, updated_at=EPOCH
        )

    def _workflow(self, workspace_id: UUID, project_id: UUID, task_id: UUID) -> NativeTaskWorkflow:
        return self.store.task_workflow(workspace_id, project_id, task_id) or NativeTaskWorkflow(
            workspace_id=workspace_id, project_id=project_id, task_id=task_id, updated_at=EPOCH
        )

    def _run(self, workspace_id: UUID, project_id: UUID, run_id: UUID) -> NativeExecutionRun:
        run = self.store.execution_run(workspace_id, project_id, run_id)
        if run is None:
            raise NotFoundError("Execution run not found.")
        return run

    def _save(self, record: _Record, **changes: Any) -> _Record:
        values = {**record.model_dump(), **changes, "version": record.version + 1}  # type: ignore[attr-defined]
        if "updated_at" in type(record).model_fields:
            values["updated_at"] = utc_now()
        updated = type(record).model_validate(values)
        methods = {
            "NativeExecutionRun": "update_execution_run",
            "NativeExecutionStep": "update_execution_step",
            "NativeExecutionWait": "update_execution_wait",
            "NativeExecutionSchedule": "update_execution_schedule",
            "NativeExecutionRunner": "update_execution_runner",
        }
        getattr(self.store, methods[type(record).__name__])(updated, record.version)  # type: ignore[attr-defined]
        return updated

    def _event(self, run: NativeExecutionRun, event_kind: str, **details: Any) -> None:
        count = 0
        while page := self.store.execution_events(
            run.workspace_id, run.project_id, run.id, count, 100
        ):
            count += len(page)
        self.store.append_execution_event(
            NativeExecutionEvent(
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                run_id=run.id,
                sequence=count + 1,
                kind=event_kind,
                details=details,
            )
        )

    @staticmethod
    def _namespace(actor: ActorContext, project_id: UUID) -> str:
        return f"native_execution:{actor.workspace_id}:{project_id}:{actor.actor_id}"

    def _once(
        self,
        actor: ActorContext,
        project_id: UUID,
        command: NativeCommand,
        kind: str,
        target: UUID,
        operation: Callable[[], UUID],
    ) -> UUID:
        output, _ = self.store.execute_once(
            self._namespace(actor, project_id),
            command.idempotency_key,
            digest(
                {
                    "kind": kind,
                    "target": str(target),
                    "command": command.model_dump(mode="json", exclude={"idempotency_key"}),
                }
            ),
            lambda: {"kind": kind, "id": str(operation())},
        )
        return UUID(output["id"])

    def operation_receipt(self, actor: ActorContext, project_id: UUID, key: str) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            receipt = self.store.command_receipt(self._namespace(actor, project_id), key)
            if receipt is None:
                raise NotFoundError("Execution operation not found.")
            return receipt

    def update_policy(
        self, actor: ActorContext, project_id: UUID, command: UpdateExecutionPolicy
    ) -> NativeExecutionPolicy:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def save() -> UUID:
                current = self._policy(actor.workspace_id, project_id)
                self.projects._version(current.version, command.expected_version)
                value = NativeExecutionPolicy(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    version=current.version + 1,
                    issued_by=actor.actor_id,
                    **command.model_dump(exclude={"expected_version", "idempotency_key"}),
                )
                self.store.save_execution_policy(value, current.version)
                self.projects.audit.record(
                    event_type="native.execution.policy_updated",
                    actor=actor,
                    resource_type="execution_policy",
                    resource_id=str(project_id),
                    payload={"version": value.version, "enabled": value.enabled},
                )
                return project_id

            self._once(actor, project_id, command, "policy", project_id, save)
            return self._policy(actor.workspace_id, project_id)

    def task_workflow(self, actor: ActorContext, project_id: UUID, task_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self.projects.get_task(actor, project_id, task_id)
            tasks = self._tasks(actor.workspace_id, project_id)
            agents = self.store.native_agents(actor.workspace_id, project_id, 0, 100)
            return {
                "config": self._workflow(actor.workspace_id, project_id, task_id).model_dump(
                    mode="json"
                ),
                "tasks": [
                    t.model_dump(mode="json", include={"id", "title", "status", "version"})
                    for t in tasks
                ],
                "agents": [
                    a.model_dump(mode="json", include={"id", "name", "status"}) for a in agents
                ],
            }

    def _tasks(self, workspace_id: UUID, project_id: UUID) -> tuple[NativeTask, ...]:
        tasks: list[NativeTask] = []
        while page := self.store.native_tasks(workspace_id, project_id, len(tasks), 100):
            tasks.extend(page)
        return tuple(tasks)

    def update_workflow(
        self, actor: ActorContext, project_id: UUID, task_id: UUID, command: UpdateTaskWorkflow
    ) -> NativeTaskWorkflow:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            self.projects.get_task(actor, project_id, task_id)

            def save() -> UUID:
                current = self._workflow(actor.workspace_id, project_id, task_id)
                self.projects._version(current.version, command.expected_version)
                graph = {
                    row.task_id: row.dependency_ids
                    for row in self.store.task_workflows(actor.workspace_id, project_id)
                }
                graph[task_id] = command.dependency_ids
                visited: set[UUID] = set()
                pending = list(command.dependency_ids)
                while pending:
                    identifier = pending.pop()
                    if identifier == task_id:
                        raise ValidationError("Task dependencies cannot contain a cycle.")
                    if identifier in visited:
                        continue
                    self.projects.get_task(actor, project_id, identifier)
                    visited.add(identifier)
                    pending.extend(graph.get(identifier, ()))
                value = NativeTaskWorkflow(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    task_id=task_id,
                    version=current.version + 1,
                    dependency_ids=command.dependency_ids,
                    not_before=command.not_before,
                )
                self.store.save_task_workflow(value, current.version)
                return task_id

            self._once(actor, project_id, command, "workflow", task_id, save)
            return self._workflow(actor.workspace_id, project_id, task_id)

    def _grant(
        self, workspace_id: UUID, project_id: UUID
    ) -> tuple[NativeExecutionPolicy, ActorContext]:
        policy = self._policy(workspace_id, project_id)
        if not policy.enabled or policy.issued_by is None:
            raise InvalidTransitionError(
                "Enable an owner-issued execution policy before starting work."
            )
        return policy, self._issuer(workspace_id, project_id, policy.issued_by)

    def _admit(
        self,
        actor: ActorContext,
        project_id: UUID,
        task: NativeTask,
        *,
        agent_id: UUID | None = None,
        parent: NativeExecutionRun | None = None,
        schedule: NativeExecutionSchedule | None = None,
        run_id: UUID | None = None,
    ) -> NativeExecutionRun:
        policy, issuer = self._grant(actor.workspace_id, project_id)
        active = self.store.execution_active_runs(actor.workspace_id, project_id)
        if len(active) >= policy.max_queued_runs or any(r.task_id == task.id for r in active):
            raise InvalidTransitionError(
                "This task already has active work or the project queue is full."
            )
        if task.status in {"done", "cancelled"}:
            raise InvalidTransitionError("Reopen the task before starting another execution.")
        if task.assignment.kind == "human":
            raise InvalidTransitionError(
                "Human-assigned work must be reassigned before agent execution."
            )
        chosen = task.assignment.agent_id or agent_id
        if chosen is None or (
            agent_id is not None and task.assignment.agent_id not in {None, agent_id}
        ):
            raise ValidationError(
                "Choose the task's assigned agent or an active agent for pooled work."
            )
        agent = self.store.native_agent(actor.workspace_id, project_id, chosen)
        if agent is None or agent.status != "active":
            raise ValidationError("Execution requires an active agent in this project.")
        project = self.projects.get_project(issuer, project_id)
        definition = self._workflow(actor.workspace_id, project_id, task.id)
        runtime = self.models.runtime(issuer, project_id)
        model = runtime.bindings[runtime.planning_endpoint_id].model
        bounds = policy
        depth = 0
        if parent is not None:
            bounds, depth = parent.bounds, parent.depth + 1
            graph = self.store.execution_root_runs(
                actor.workspace_id, project_id, parent.root_run_id
            )
            if depth > bounds.max_depth or len(graph) - 1 >= bounds.max_children:
                raise InvalidTransitionError(
                    "The root workflow's delegation allowance is exhausted."
                )
            model = self.models.resolve(issuer, project_id, parent.model_id).model
        task = self.projects.update_task(
            issuer,
            project_id,
            task.id,
            UpdateNativeTask(
                expected_version=task.version,
                idempotency_key=f"execution-assignment-{run_id or uuid4()}",
                title=task.title,
                description=task.description,
                status="in_progress",
                assignment=TaskAssignment(kind="agent", agent_id=chosen),
            ),
        )
        identifier = run_id or uuid4()
        context = {
            "project": {"name": project.name, "objective": project.objective},
            "task": {"title": task.title, "description": task.description},
            "task_digest": task_digest(task),
            "role": {
                "name": agent.name,
                "instructions": agent.instructions,
                "success_criteria": agent.success_criteria,
            },
            "dependency_ids": [str(i) for i in definition.dependency_ids],
            "available_agents": [
                {
                    "id": str(a.id),
                    "name": a.name,
                    "role_key": a.role_key,
                    "success_criteria": a.success_criteria[:200],
                }
                for a in self.store.native_agents(actor.workspace_id, project_id, 0, 100)
                if a.status == "active"
            ],
        }
        # Refuse oversized snapshots as a domain validation error before changing
        # execution state; ordinary API callers must never receive a schema 500.
        try:
            bounded_json(context, 131072)
        except ValueError as exc:
            raise ValidationError(
                "Execution context is too large; narrow this task's brief."
            ) from exc
        started = utc_now()
        run = NativeExecutionRun(
            id=identifier,
            workspace_id=actor.workspace_id,
            project_id=project_id,
            task_id=task.id,
            agent_id=chosen,
            issued_by=issuer.actor_id,
            root_run_id=parent.root_run_id if parent else identifier,
            parent_run_id=parent.id if parent else None,
            schedule_id=schedule.id if schedule else None,
            schedule_definition_version=schedule.definition_version if schedule else None,
            task_version=task.version,
            agent_version=agent.version,
            project_version=project.version,
            workflow_version=definition.version,
            policy_version=policy.version,
            input_digest=digest(context),
            context=context,
            bounds=bounds,
            model_id=model.id,
            depth=depth,
            next_wake_at=definition.not_before,
            created_at=started,
            deadline_at=min(
                started + timedelta(seconds=bounds.run_timeout_seconds), parent.deadline_at
            )
            if parent
            else started + timedelta(seconds=bounds.run_timeout_seconds),
        )
        self.store.insert_execution_run(run)
        self._event(
            run,
            "queued",
            task_id=str(task.id),
            agent_id=str(chosen),
            parent_run_id=str(parent.id) if parent else None,
        )
        return run

    def start(
        self, actor: ActorContext, project_id: UUID, task_id: UUID, command: StartExecution
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def create() -> UUID:
                task = self.projects.get_task(actor, project_id, task_id)
                self.projects._version(task.version, command.expected_task_version)
                return self._admit(actor, project_id, task, agent_id=command.agent_id).id

            identifier = self._once(actor, project_id, command, "run", task_id, create)
            return public_run(self._run(actor.workspace_id, project_id, identifier))

    def _authority(self, run: NativeExecutionRun) -> ActorContext:
        actor = self._issuer(run.workspace_id, run.project_id, run.issued_by)
        policy = self._policy(run.workspace_id, run.project_id)
        if (
            not policy.enabled
            or policy.version != run.policy_version
            or policy.issued_by != run.issued_by
        ):
            raise InvalidTransitionError("The execution grant changed.")
        if run.deadline_at <= utc_now():
            raise InvalidTransitionError("The workflow deadline expired.")
        project = self.projects.get_project(actor, run.project_id)
        task = self.projects.get_task(actor, run.project_id, run.task_id)
        agent = self.store.native_agent(run.workspace_id, run.project_id, run.agent_id)
        workflow = self._workflow(run.workspace_id, run.project_id, run.task_id)
        if (
            project.version != run.project_version
            or task.version != run.task_version
            or task.status != "in_progress"
            or task_digest(task) != run.context["task_digest"]
            or agent is None
            or agent.status != "active"
            or agent.version != run.agent_version
            or workflow.version != run.workflow_version
        ):
            raise InvalidTransitionError(
                "Task, project, role or dependencies changed during execution."
            )
        if run.schedule_id:
            schedule = self.store.execution_schedule(
                run.workspace_id, run.project_id, run.schedule_id
            )
            if (
                schedule is None
                or not schedule.enabled
                or schedule.definition_version != run.schedule_definition_version
            ):
                raise InvalidTransitionError("The scheduling grant changed.")
            self._issuer(run.workspace_id, run.project_id, schedule.issued_by)
        if run.parent_run_id:
            parent = self._run(run.workspace_id, run.project_id, run.parent_run_id)
            if parent.status in {"cancelled", "stale", "failed", "unknown"}:
                raise InvalidTransitionError("The parent workflow cannot continue.")
            self._authority(parent)
        self.models.resolve(actor, run.project_id, run.model_id)
        return actor

    def _dependencies(self, run: NativeExecutionRun) -> tuple[dict[str, Any], ...] | None:
        results = []
        for value in run.context["dependency_ids"]:
            task_id = UUID(value)
            task = self.store.native_task(run.workspace_id, run.project_id, task_id)
            if task is None or task.status == "cancelled":
                return None
            candidates = self.store.execution_task_runs(
                run.workspace_id, run.project_id, task_id, 0, 50
            )
            match = next(
                (
                    r
                    for r in candidates
                    if r.status == "completed" and r.context.get("task_digest") == task_digest(task)
                ),
                None,
            )
            if match is None:
                return None
            results.append(
                {
                    "task_id": str(task_id),
                    "run_id": str(match.id),
                    "task_digest": task_digest(task),
                    "title": task.title,
                    "candidate": match.result_text,
                }
            )
        return tuple(results)

    def totals(self, run: NativeExecutionRun) -> dict[str, int]:
        charged = held = calls = 0
        for step in self.store.execution_root_steps(
            run.workspace_id, run.project_id, run.root_run_id
        ):
            if step.kind != "model":
                continue
            calls += 1
            outcome = self.usage.operation_outcome(
                run.workspace_id, run.project_id, step.operation_id
            )
            charged += outcome.charged_microusd
            held += outcome.held_microusd
        return {"charged_microusd": charged, "held_microusd": held, "model_calls": calls}

    def list_runs(
        self, actor: ActorContext, project_id: UUID, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self.projects._page(offset, limit)
            rows = self.store.execution_runs(actor.workspace_id, project_id, offset, limit)
            return {
                "items": [public_run(r) for r in rows],
                "has_more": bool(
                    self.store.execution_runs(actor.workspace_id, project_id, offset + limit, 1)
                ),
            }

    def list_schedules(
        self, actor: ActorContext, project_id: UUID, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self.projects._page(offset, limit)
            rows = self.store.execution_schedules(actor.workspace_id, project_id, offset, limit)
            return {
                "items": [r.model_dump(mode="json") for r in rows],
                "has_more": bool(
                    self.store.execution_schedules(
                        actor.workspace_id, project_id, offset + limit, 1
                    )
                ),
            }

    def events(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self._run(actor.workspace_id, project_id, run_id)
            self.projects._page(offset, limit)
            return {
                "items": [
                    e.model_dump(mode="json")
                    for e in self.store.execution_events(
                        actor.workspace_id, project_id, run_id, offset, limit
                    )
                ],
                "has_more": bool(
                    self.store.execution_events(
                        actor.workspace_id, project_id, run_id, offset + limit, 1
                    )
                ),
            }

    def create_schedule(
        self, actor: ActorContext, project_id: UUID, task_id: UUID, command: CreateExecutionSchedule
    ) -> NativeExecutionSchedule:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def create() -> UUID:
                task = self.projects.get_task(actor, project_id, task_id)
                self.projects._version(task.version, command.expected_task_version)
                self._grant(actor.workspace_id, project_id)
                if task.assignment.kind != "agent":
                    raise ValidationError("A scheduled task must have an assigned agent.")
                schedule = NativeExecutionSchedule(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    task_id=task_id,
                    issued_by=actor.actor_id,
                    **command.model_dump(exclude={"expected_task_version", "idempotency_key"}),
                )
                self.store.insert_execution_schedule(schedule)
                return schedule.id

            identifier = self._once(actor, project_id, command, "schedule", task_id, create)
            result = self.store.execution_schedule(actor.workspace_id, project_id, identifier)
            assert result is not None
            return result

    def update_schedule(
        self,
        actor: ActorContext,
        project_id: UUID,
        schedule_id: UUID,
        command: UpdateExecutionSchedule,
    ) -> NativeExecutionSchedule:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def update() -> UUID:
                current = self.store.execution_schedule(actor.workspace_id, project_id, schedule_id)
                if current is None:
                    raise NotFoundError("Schedule not found.")
                self.projects._version(current.version, command.expected_version)
                if command.max_occurrences < current.occurrence_count:
                    raise ValidationError("Schedule history cannot be reset.")
                self._save(
                    current,
                    definition_version=current.definition_version + 1,
                    **command.model_dump(exclude={"expected_version", "idempotency_key"}),
                )
                return current.id

            self._once(actor, project_id, command, "schedule-update", schedule_id, update)
            result = self.store.execution_schedule(actor.workspace_id, project_id, schedule_id)
            assert result is not None
            return result

    def enroll_runner(
        self, actor: ActorContext, project_id: UUID, command: EnrollExecutionRunner
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            token: str | None = None

            def create() -> UUID:
                nonlocal token
                current = self.store.execution_runners(actor.workspace_id, project_id)
                if sum(r.status == "active" and r.expires_at > utc_now() for r in current) >= 32:
                    raise InvalidTransitionError(
                        "Revoke an existing runner before enrolling another."
                    )
                token = "simon_runner_" + secrets.token_urlsafe(32)
                runner = NativeExecutionRunner(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    name=command.name,
                    issued_by=actor.actor_id,
                    token_hash=token_hash(token),
                    max_concurrent_runs=command.max_concurrent_runs,
                    expires_at=utc_now() + timedelta(seconds=command.ttl_seconds),
                )
                self.store.insert_execution_runner(runner)
                return runner.id

            identifier = self._once(actor, project_id, command, "runner", project_id, create)
            runner = self.store.execution_runner(actor.workspace_id, project_id, identifier)
            assert runner is not None
            return {"runner": public_runner(runner), "token": token, "replayed": token is None}

    def revoke_runner(
        self,
        actor: ActorContext,
        project_id: UUID,
        runner_id: UUID,
        command: VersionedNativeCommand,
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def revoke() -> UUID:
                runner = self.store.execution_runner(actor.workspace_id, project_id, runner_id)
                if runner is None:
                    raise NotFoundError("Runner not found.")
                self.projects._version(runner.version, command.expected_version)
                self._save(runner, status="revoked")
                return runner.id

            self._once(actor, project_id, command, "runner-revoke", runner_id, revoke)
            result = self.store.execution_runner(actor.workspace_id, project_id, runner_id)
            assert result is not None
            return public_runner(result)

    def runner(self, token: str) -> NativeExecutionRunner:
        if not token.startswith("simon_runner_") or len(token) > 256:
            raise AuthorizationError("A valid execution runner credential is required.")
        record = self.store.execution_runner_by_hash(token_hash(token))
        if record is None:
            raise AuthorizationError("A valid execution runner credential is required.")
        self._check_runner(record)
        return record

    def _check_runner(self, runner: NativeExecutionRunner) -> NativeExecutionRunner:
        current = self.store.execution_runner(runner.workspace_id, runner.project_id, runner.id)
        if (
            current is None
            or current.status != "active"
            or current.expires_at <= utc_now()
            or current.token_hash != runner.token_hash
        ):
            raise AuthorizationError("Execution runner authority expired or was revoked.")
        self._issuer(current.workspace_id, current.project_id, current.issued_by)
        return current

    @staticmethod
    def correlation(run: NativeExecutionRun, step: int | None = None) -> UUID:
        return uuid5(run.id, f"answer:{step or run.step_number + 1}")
