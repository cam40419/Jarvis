"""Bounded native execution with durable grants, checkpoints and leased workers."""

import json
import secrets
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError as ModelValidationError

from simon.adapters.model_endpoints import ModelEndpointError
from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, utc_now
from simon.domain.native_execution import (
    ExecutionCommand,
    NativeExecutionRun,
    NativeExecutionRunner,
    NativeExecutionSchedule,
    NativeExecutionSignal,
    NativeExecutionStep,
    NativeExecutionWait,
    RunnerLeaseCommand,
    SignalExecution,
)
from simon.domain.native_models import ModelUsage
from simon.domain.native_projects import (
    CreateNativeTask,
    NativeCommand,
    TaskAssignment,
    UpdateNativeTask,
)
from simon.domain.ports import Store
from simon.services.canonical import digest
from simon.services.identity import IDENTITY_LOCK
from simon.services.native_projects import NativeProjectService
from simon.services.native_workflows import (
    LIVE,
    NativeWorkflowService,
    public_run,
    public_runner,
    task_digest,
    token_hash,
)
from simon.services.project_models import ProjectModelService
from simon.services.reference_work import ReferenceWork
from simon.services.workflow_executor import WorkflowAction, WorkflowExecutor, WorkflowPlanningError

_AUTHORITY_ERRORS = (AuthorizationError, InvalidTransitionError, NotFoundError, ValidationError)


class NativeExecutionService(NativeWorkflowService):
    def __init__(
        self, store: Store, projects: NativeProjectService, models: ProjectModelService
    ) -> None:
        super().__init__(store, projects, models)
        self.reference = ReferenceWork(store)

    def _unlease(self, run: NativeExecutionRun, **changes: Any) -> NativeExecutionRun:
        return self._save(run, runner_id=None, lease_token_hash=None, lease_until=None, **changes)

    def _finish(
        self, run: NativeExecutionRun, status: str, code: str | None = None
    ) -> NativeExecutionRun:
        if run.status in {"completed", "cancelled", "stale"}:
            return run
        for step in self.store.execution_steps(run.workspace_id, run.project_id, run.id):
            if step.usage_id:
                self.usage.release(run.workspace_id, run.project_id, step.usage_id)
        for wait in self.store.execution_waits(run.workspace_id, run.project_id, run.id):
            if wait.status == "pending":
                self._save(wait, status="cancelled", resolved_at=utc_now())
        result = self._unlease(
            run, status=status, error_code=code, finished_at=utc_now(), next_wake_at=None
        )
        self._event(result, status, error_code=code)
        return result

    def _recover(self, workspace_id: UUID, project_id: UUID) -> None:
        self.usage.recover(workspace_id)
        now = utc_now()
        for run in self.store.execution_active_runs(workspace_id, project_id):
            if run.deadline_at <= now:
                for wait in self.store.execution_waits(workspace_id, project_id, run.id):
                    if wait.status == "pending":
                        self._save(wait, status="timed_out", resolved_at=now)
                self._finish(run, "failed", "workflow_deadline_expired")
                continue
            try:
                self._authority(run)
            except _AUTHORITY_ERRORS:
                self._finish(run, "stale", "execution_authority_changed")
                continue
            if run.status == "running" and run.lease_until is not None and run.lease_until <= now:
                steps = self.store.execution_steps(workspace_id, project_id, run.id)
                pending = next(
                    (
                        s
                        for s in steps
                        if s.sequence > run.applied_step and s.status in {"dispatched", "unknown"}
                    ),
                    None,
                )
                if pending is not None:
                    if pending.status == "dispatched":
                        self._save(
                            pending,
                            status="unknown",
                            error_code="worker_interrupted",
                            finished_at=now,
                        )
                    self._finish(run, "unknown", "worker_interrupted")
                elif run.attempt >= run.bounds.max_attempts:
                    self._finish(run, "failed", "attempt_limit_reached")
                else:
                    self._unlease(
                        run,
                        status="queued",
                        attempt=run.attempt + 1,
                        error_code="lease_expired",
                    )
                    self._event(run, "lease_expired", fence=run.fence)
            elif run.status == "waiting":
                self._wake(run)

    def _wake(self, run: NativeExecutionRun) -> NativeExecutionRun:
        wait = next(
            (
                w
                for w in self.store.execution_waits(run.workspace_id, run.project_id, run.id)
                if w.status == "pending"
            ),
            None,
        )
        if wait is None:
            return self._unlease(run, status="queued", next_wake_at=None)
        now = utc_now()
        answer = self.store.execution_signal(
            run.workspace_id, run.project_id, run.id, wait.correlation_id
        )
        ready = wait.kind == "human" and answer is not None
        if wait.kind == "timer":
            ready = wait.deadline_at <= now
        if wait.kind == "children":
            children = [
                r
                for r in self.store.execution_root_runs(
                    run.workspace_id, run.project_id, run.root_run_id
                )
                if r.parent_run_id == run.id
            ]
            if any(r.status in {"failed", "unknown", "cancelled", "stale"} for r in children):
                # A parent must remain resumable while its owner reconciles or retries
                # a child. Making it terminal here would revoke every child's grant.
                if wait.deadline_at <= now:
                    self._save(wait, status="timed_out", resolved_at=now)
                    return self._finish(run, "failed", "wait_expired")
                return (
                    self._save(run, error_code="child_requires_attention")
                    if run.error_code != "child_requires_attention"
                    else run
                )
            ready = bool(children) and all(r.status == "completed" for r in children)
        if ready:
            self._save(
                wait,
                status="resolved",
                response=answer.text if answer else "",
                resolved_by=answer.received_by if answer else None,
                resolved_at=now,
            )
            self._event(run, "resumed", wait_id=str(wait.id), kind=wait.kind)
            return self._unlease(run, status="queued", next_wake_at=None, error_code=None)
        if wait.deadline_at <= now:
            self._save(wait, status="timed_out", resolved_at=now)
            return self._finish(run, "failed", "wait_expired")
        return run

    def _all_schedules(
        self, workspace_id: UUID, project_id: UUID
    ) -> Iterator[NativeExecutionSchedule]:
        offset = 0
        while page := self.store.execution_schedules(workspace_id, project_id, offset, 100):
            yield from page
            offset += len(page)

    def _tick(self, workspace_id: UUID, project_id: UUID) -> None:
        self._recover(workspace_id, project_id)
        try:
            policy, issuer = self._grant(workspace_id, project_id)
        except _AUTHORITY_ERRORS:
            return
        now = utc_now()
        for schedule in self._all_schedules(workspace_id, project_id):
            if (
                not schedule.enabled
                or schedule.next_run_at is None
                or schedule.next_run_at > now
                or schedule.occurrence_count >= schedule.max_occurrences
            ):
                continue
            try:
                owner = self._issuer(workspace_id, project_id, schedule.issued_by)
                if (
                    schedule.last_run_id
                    and self._run(workspace_id, project_id, schedule.last_run_id).status in LIVE
                ):
                    continue
                template = self.projects.get_task(owner, project_id, schedule.task_id)
                if template.assignment.kind != "agent" or template.status == "cancelled":
                    raise InvalidTransitionError("The scheduled template is unavailable.")
                occurrence = schedule.next_run_at.isoformat()
                occurrence_id = uuid5(schedule.id, occurrence)
                # Admission and occurrence advancement commit together, including the
                # separate board task. A failed admission leaves no orphan occurrence.
                with self.store.transaction(workspace_id):
                    task = self.projects.create_task(
                        owner,
                        project_id,
                        CreateNativeTask(
                            title=(template.title + " · " + occurrence[:16])[:200],
                            description=template.description,
                            assignment=template.assignment,
                            idempotency_key=f"schedule:{schedule.id}:{occurrence}",
                        ),
                    )
                    definition = self._workflow(workspace_id, project_id, template.id)
                    if definition.version:
                        self.store.save_task_workflow(
                            definition.model_copy(
                                update={"task_id": task.id, "version": 1, "not_before": None}
                            ),
                            0,
                        )
                    run = self._admit(
                        owner, project_id, task, schedule=schedule, run_id=occurrence_id
                    )
                    count = schedule.occurrence_count + 1
                    following = None
                    if schedule.interval_seconds and count < schedule.max_occurrences:
                        jumps = (
                            int((now - schedule.next_run_at).total_seconds())
                            // schedule.interval_seconds
                            + 1
                        )
                        following = schedule.next_run_at + timedelta(
                            seconds=jumps * schedule.interval_seconds
                        )
                    self._save(
                        schedule,
                        occurrence_count=count,
                        last_run_id=run.id,
                        last_occurrence_at=schedule.next_run_at,
                        next_run_at=following,
                    )
            except _AUTHORITY_ERRORS:
                # Capacity/model outages don't fabricate a fire or silently discard
                # a schedule. The next poll rechecks its current grant and coalesces.
                continue
        if not policy.auto_start:
            return
        for task in self._tasks(workspace_id, project_id):
            if task.status != "todo" or task.assignment.kind != "agent":
                continue
            if (
                len(self.store.execution_active_runs(workspace_id, project_id))
                >= policy.max_queued_runs
            ):
                break
            latest = self.store.execution_task_runs(workspace_id, project_id, task.id, 0, 1)
            if latest and latest[0].context.get("task_digest") == task_digest(task):
                continue
            try:
                with self.store.transaction(workspace_id):
                    self._admit(issuer, project_id, task)
            except _AUTHORITY_ERRORS:
                continue

    def view(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self._recover(actor.workspace_id, project_id)
            manageable = True
            try:
                self._access(actor, project_id, write=True)
            except _AUTHORITY_ERRORS:
                manageable = False
            reason = ""
            try:
                _, issuer = self._grant(actor.workspace_id, project_id)
                self.models.runtime(issuer, project_id)
            except _AUTHORITY_ERRORS as exc:
                reason = str(exc)
            active = {
                r.task_id for r in self.store.execution_active_runs(actor.workspace_id, project_id)
            }
            candidates = []
            for task in self._tasks(actor.workspace_id, project_id):
                issue = reason
                if task.id in active:
                    issue = "This task already has active execution."
                elif task.status in {"done", "cancelled"}:
                    issue = "Reopen the task before executing it."
                elif task.assignment.kind == "human":
                    issue = "Human-assigned work must first be reassigned."
                candidates.append(
                    {
                        "task_id": str(task.id),
                        "task_version": task.version,
                        "title": task.title,
                        "agent_id": str(task.assignment.agent_id)
                        if task.assignment.agent_id
                        else None,
                        "eligible": manageable and not issue,
                        "reason": issue,
                    }
                )
            return {
                "policy": self._policy(actor.workspace_id, project_id).model_dump(mode="json"),
                "runs": self.list_runs(actor, project_id),
                "schedules": self.list_schedules(actor, project_id),
                "runners": [
                    public_runner(r)
                    for r in self.store.execution_runners(actor.workspace_id, project_id)
                ],
                "can_manage": manageable,
                "candidates": candidates,
                "worker": {"mode": "outbound"},
            }

    def detail(self, actor: ActorContext, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self._recover(actor.workspace_id, project_id)
            run = self._run(actor.workspace_id, project_id, run_id)
            manageable = True
            try:
                self._access(actor, project_id, write=True)
            except _AUTHORITY_ERRORS:
                manageable = False
            retry = (
                manageable
                and run.status in {"failed", "unknown"}
                and run.attempt < run.bounds.max_attempts
            )
            try:
                self._authority(run)
            except _AUTHORITY_ERRORS:
                retry = False
            steps = self.store.execution_steps(actor.workspace_id, project_id, run_id)
            if any(
                s.usage_id
                and (usage := self.store.model_usage(actor.workspace_id, project_id, s.usage_id))
                and usage.status in {"reserved", "dispatched", "unknown"}
                for s in steps
            ):
                retry = False
            waits = self.store.execution_waits(actor.workspace_id, project_id, run_id)
            pending = next((w for w in waits if w.status == "pending" and w.kind == "human"), None)
            events = self.events(actor, project_id, run_id)
            return {
                "run": public_run(run),
                "events": events["items"],
                "events_has_more": events["has_more"],
                "steps": [s.model_dump(mode="json", exclude={"request"}) for s in steps],
                "waits": [w.model_dump(mode="json") for w in waits],
                "children": [
                    public_run(r)
                    for r in self.store.execution_root_runs(
                        actor.workspace_id, project_id, run.root_run_id
                    )
                    if r.parent_run_id == run.id
                ],
                "totals": self.totals(run),
                "can_cancel": manageable and run.status in LIVE,
                "can_retry": retry,
                "can_signal": manageable
                and (pending is not None or run.status in {"queued", "running"}),
                "correlation_id": str(
                    pending.correlation_id
                    if pending
                    else self.correlation(
                        run, run.step_number if run.step_number > run.applied_step else None
                    )
                ),
            }

    def cancel(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, command: ExecutionCommand
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def stop() -> UUID:
                run = self._run(actor.workspace_id, project_id, run_id)
                self.projects._version(run.version, command.expected_version)
                if run.status not in LIVE:
                    raise InvalidTransitionError("Only unfinished execution can be cancelled.")
                descendants = {run.id}
                graph = self.store.execution_root_runs(
                    actor.workspace_id, project_id, run.root_run_id
                )
                for _ in range(run.bounds.max_depth + 1):
                    descendants.update(r.id for r in graph if r.parent_run_id in descendants)
                for current in graph:
                    if current.id in descendants and current.status in LIVE:
                        self._finish(current, "cancelled", "cancelled_by_owner")
                return run.id

            self._once(actor, project_id, command, "cancel", run_id, stop)
            return public_run(self._run(actor.workspace_id, project_id, run_id))

    def retry(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, command: ExecutionCommand
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def resume() -> UUID:
                self._recover(actor.workspace_id, project_id)
                run = self._run(actor.workspace_id, project_id, run_id)
                self.projects._version(run.version, command.expected_version)
                self._authority(run)
                if (
                    run.status not in {"failed", "unknown"}
                    or run.attempt >= run.bounds.max_attempts
                ):
                    raise InvalidTransitionError("This workflow cannot be retried.")
                applied = run.applied_step
                for step in self.store.execution_steps(actor.workspace_id, project_id, run_id):
                    if step.usage_id:
                        usage = self.store.model_usage(
                            actor.workspace_id, project_id, step.usage_id
                        )
                        if usage is not None and usage.status in {
                            "reserved",
                            "dispatched",
                            "unknown",
                        }:
                            raise InvalidTransitionError(
                                "Reconcile uncertain model usage before retrying."
                            )
                    if step.kind == "reference" and step.status in {"dispatched", "unknown"}:
                        receipt = self.reference.lookup(
                            actor.workspace_id, project_id, step.operation_id
                        )
                        if receipt is None:
                            raise InvalidTransitionError(
                                "The reference operation still has no definitive receipt."
                            )
                        self._save(
                            step,
                            status="completed",
                            result=receipt,
                            finished_at=utc_now(),
                            error_code=None,
                        )
                    elif step.status in {"failed", "unknown"}:
                        applied = max(applied, step.sequence)
                self._unlease(
                    run,
                    status="queued",
                    attempt=run.attempt + 1,
                    error_code=None,
                    finished_at=None,
                    applied_step=applied,
                )
                self._event(
                    run,
                    "retry_authorized",
                    actor_id=str(actor.actor_id),
                    retained_totals=self.totals(run),
                )
                return run.id

            self._once(actor, project_id, command, "retry", run_id, resume)
            return public_run(self._run(actor.workspace_id, project_id, run_id))

    def signal(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, command: SignalExecution
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def receive() -> UUID:
                run = self._run(actor.workspace_id, project_id, run_id)
                self._authority(run)
                if run.status not in {"queued", "running", "waiting"}:
                    raise InvalidTransitionError("This workflow is not accepting input.")
                pending = next(
                    (
                        w
                        for w in self.store.execution_waits(actor.workspace_id, project_id, run_id)
                        if w.status == "pending"
                    ),
                    None,
                )
                expected = (
                    pending.correlation_id
                    if pending
                    else self.correlation(
                        run, run.step_number if run.step_number > run.applied_step else None
                    )
                )
                if command.correlation_id != expected or (
                    pending and (pending.kind != "human" or pending.deadline_at <= utc_now())
                ):
                    raise InvalidTransitionError(
                        "The reply belongs to another or expired workflow wait."
                    )
                current = self.store.execution_signal(
                    actor.workspace_id, project_id, run_id, command.correlation_id
                )
                if current is not None:
                    if current.text != command.text:
                        raise InvalidTransitionError(
                            "This correlated reply was already recorded differently."
                        )
                    return run.id
                self.store.insert_execution_signal(
                    NativeExecutionSignal(
                        workspace_id=actor.workspace_id,
                        project_id=project_id,
                        run_id=run_id,
                        correlation_id=command.correlation_id,
                        received_by=actor.actor_id,
                        text=command.text,
                    )
                )
                self._event(
                    run,
                    "reply_received",
                    correlation_id=str(command.correlation_id),
                    actor_id=str(actor.actor_id),
                )
                if run.status == "waiting":
                    self._wake(run)
                return run.id

            self._once(actor, project_id, command, "signal", run_id, receive)
            return public_run(self._run(actor.workspace_id, project_id, run_id))

    def claim(self, runner: NativeExecutionRunner, command: NativeCommand) -> dict[str, Any] | None:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(runner.workspace_id):
            runner = self._check_runner(runner)
            namespace = f"execution_claim:{runner.id}"
            if self.store.command_receipt(namespace, command.idempotency_key) is not None:
                return None
            self._tick(runner.workspace_id, runner.project_id)
            lease: dict[str, Any] | None = None

            def take() -> dict[str, Any]:
                nonlocal lease
                active = self.store.execution_active_runs(runner.workspace_id, runner.project_id)
                policy = self._policy(runner.workspace_id, runner.project_id)
                running = [r for r in active if r.status == "running"]
                if (
                    len(running) >= policy.max_active_runs
                    or sum(r.runner_id == runner.id for r in running) >= runner.max_concurrent_runs
                ):
                    return {"run_id": None}
                for run in sorted(active, key=lambda r: (r.created_at, str(r.id))):
                    if run.status != "queued" or (
                        run.next_wake_at and run.next_wake_at > utc_now()
                    ):
                        continue
                    try:
                        self._authority(run)
                        if self._dependencies(run) is None:
                            continue
                    except _AUTHORITY_ERRORS:
                        self._finish(run, "stale", "execution_authority_changed")
                        continue
                    if run.attempt > run.bounds.max_attempts:
                        self._finish(run, "failed", "attempt_limit_reached")
                        continue
                    secret = secrets.token_urlsafe(32)
                    current = self._save(
                        run,
                        status="running",
                        attempt=max(1, run.attempt),
                        fence=run.fence + 1,
                        runner_id=runner.id,
                        lease_token_hash=token_hash(secret),
                        error_code=None,
                        finished_at=None,
                        lease_until=min(
                            utc_now() + timedelta(seconds=run.bounds.lease_seconds), run.deadline_at
                        ),
                    )
                    self._event(
                        current,
                        "leased",
                        runner_id=str(runner.id),
                        fence=current.fence,
                        attempt=current.attempt,
                    )
                    assert current.lease_until is not None
                    lease = {
                        "run_id": str(run.id),
                        "fence": current.fence,
                        "lease_token": secret,
                        "lease_until": current.lease_until.isoformat(),
                        "project_id": str(run.project_id),
                        "task_title": run.context["task"]["title"],
                        "agent_name": run.context["role"]["name"],
                    }
                    return {"run_id": str(run.id)}
                return {"run_id": None}

            receipt = take()
            if lease is not None:
                # Persist admitted work, not every empty polling request. Selection
                # and its safe receipt remain atomic under the same project lock.
                self.store.execute_once(
                    namespace,
                    command.idempotency_key,
                    digest({"runner": str(runner.id)}),
                    lambda: receipt,
                )
            now = utc_now()
            if runner.last_seen_at is None or runner.last_seen_at <= now - timedelta(seconds=30):
                self._save(runner, last_seen_at=now)
            return lease

    def _lease(
        self, runner: NativeExecutionRunner, command: RunnerLeaseCommand
    ) -> NativeExecutionRun:
        self._check_runner(runner)
        run = self._run(runner.workspace_id, runner.project_id, command.run_id)
        if (
            run.status != "running"
            or run.runner_id != runner.id
            or run.fence != command.fence
            or run.lease_until is None
            or run.lease_until <= utc_now()
            or not secrets.compare_digest(
                run.lease_token_hash or "", token_hash(command.lease_token)
            )
        ):
            raise InvalidTransitionError("This worker lease is expired or no longer current.")
        self._authority(run)
        return run

    def heartbeat(
        self, runner: NativeExecutionRunner, command: RunnerLeaseCommand
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(runner.workspace_id):
            run = self._lease(runner, command)
            result = self._save(
                run,
                lease_until=min(
                    utc_now() + timedelta(seconds=run.bounds.lease_seconds), run.deadline_at
                ),
            )
            assert result.lease_until is not None
            return {
                "run_id": str(run.id),
                "fence": run.fence,
                "lease_until": result.lease_until.isoformat(),
            }

    def worker_status(self, runner: NativeExecutionRunner, run_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(runner.workspace_id):
            self._check_runner(runner)
            self._recover(runner.workspace_id, runner.project_id)
            return public_run(self._run(runner.workspace_id, runner.project_id, run_id))

    def _history(self, run: NativeExecutionRun) -> tuple[dict[str, Any], ...]:
        history: list[dict[str, Any]] = [
            {"kind": step.kind, "sequence": step.sequence, "result": step.result}
            for step in self.store.execution_steps(run.workspace_id, run.project_id, run.id)
            if step.status == "completed"
        ]
        history.extend(
            {"kind": "human_reply", "question": wait.question, "answer": wait.response}
            for wait in self.store.execution_waits(run.workspace_id, run.project_id, run.id)
            if wait.status == "resolved" and wait.kind == "human"
        )
        history.extend(
            {
                "kind": "child_candidate",
                "run_id": str(child.id),
                "sha256": digest(child.result_text),
                "preview": child.result_text[:3000],
            }
            for child in self.store.execution_root_runs(
                run.workspace_id, run.project_id, run.root_run_id
            )
            if child.parent_run_id == run.id and child.status == "completed"
        )
        return tuple(history)

    def step(self, runner: NativeExecutionRunner, command: RunnerLeaseCommand) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(runner.workspace_id):
            run = self._lease(runner, command)
            namespace = f"execution_step_command:{run.id}:{run.fence}"
            receipt = self.store.command_receipt(namespace, command.idempotency_key)
            if receipt is not None:
                return public_run(run)
            pending = next(
                (
                    s
                    for s in self.store.execution_steps(run.workspace_id, run.project_id, run.id)
                    if s.sequence > run.applied_step
                ),
                None,
            )
            if pending is not None and pending.status == "completed":
                self.store.execute_once(
                    namespace,
                    command.idempotency_key,
                    digest({"run_id": str(run.id), "fence": run.fence}),
                    lambda: {"sequence": pending.sequence},
                )
                return public_run(self._advance_checkpoint(run, pending))
            if pending is not None and not (
                pending.kind == "reference" and pending.status == "prepared"
            ):
                raise InvalidTransitionError(
                    "This step is already dispatched or needs explicit recovery."
                )
            if pending is None and run.step_number >= run.bounds.max_steps:
                return public_run(self._finish(run, "failed", "step_limit_reached"))
            self.store.execute_once(
                namespace,
                command.idempotency_key,
                digest({"run_id": str(run.id), "fence": run.fence}),
                lambda: {"sequence": pending.sequence if pending else run.step_number + 1},
            )
            if pending is not None:
                current_step = self._save(pending, status="dispatched")
                executor = prepared = None
            else:
                actor = self._authority(run)
                dependencies = self._dependencies(run)
                if dependencies is None:
                    raise InvalidTransitionError(
                        "A required task candidate is no longer available."
                    )
                binding = self.models.resolve(actor, run.project_id, run.model_id)
                executor = WorkflowExecutor(binding, transport=self.models.transport)
                totals = self.totals(run)
                graph = self.store.execution_root_runs(
                    run.workspace_id, run.project_id, run.root_run_id
                )
                context = {
                    **run.context,
                    "dependencies": [
                        {**d, "candidate": d["candidate"][:3000]} for d in dependencies
                    ],
                    "execution": {
                        "step_number": run.step_number + 1,
                        "steps_remaining": max(0, run.bounds.max_steps - run.step_number),
                        "depth": run.depth,
                        "max_depth": run.bounds.max_depth,
                        "children_remaining": max(0, run.bounds.max_children - (len(graph) - 1)),
                        "model_calls_remaining": max(
                            0, run.bounds.max_model_calls - totals["model_calls"]
                        ),
                        "cost_remaining_microusd": max(
                            0,
                            run.bounds.max_cost_microusd
                            - totals["charged_microusd"]
                            - totals["held_microusd"],
                        ),
                        "deadline_at": run.deadline_at.isoformat(),
                        "attempt": run.attempt,
                        "attempts_remaining": max(0, run.bounds.max_attempts - run.attempt),
                    },
                }
                try:
                    prepared = executor.prepare(context, self._history(run))
                    if (
                        totals["model_calls"] >= run.bounds.max_model_calls
                        or totals["charged_microusd"]
                        + totals["held_microusd"]
                        + prepared.reservation_microusd
                        > run.bounds.max_cost_microusd
                    ):
                        raise InvalidTransitionError(
                            "The root workflow resource allowance is exhausted."
                        )
                    now, operation_id = utc_now(), uuid4()
                    usage = ModelUsage(
                        workspace_id=run.workspace_id,
                        project_id=run.project_id,
                        operation_id=operation_id,
                        phase="execution",
                        requested_by=run.issued_by,
                        model_id=binding.model.id,
                        model_version=binding.model.version,
                        credential_revision=binding.model.credential_revision,
                        template_id=binding.model.template_id,
                        model=binding.endpoint.model,
                        endpoint_fingerprint=binding.fingerprint,
                        endpoint_snapshot=binding.endpoint.model_dump(mode="json"),
                        input_rate=prepared.input_rate,
                        output_rate=prepared.output_rate,
                        reserved_microusd=prepared.reservation_microusd,
                        held_microusd=prepared.reservation_microusd,
                        started_at=now,
                        deadline_at=now + timedelta(seconds=run.bounds.lease_seconds),
                    )
                    with self.store.transaction(run.workspace_id):
                        self.usage.reserve(actor, run.project_id, operation_id, (usage,))
                        self.models.assert_current(actor, run.project_id, binding)
                        self.usage.dispatch(actor, run.project_id, usage.id)
                except (ModelEndpointError, *_AUTHORITY_ERRORS) as exc:
                    return public_run(
                        self._finish(
                            run, "failed", getattr(exc, "code", "resource_admission_blocked")
                        )
                    )
                current_step = NativeExecutionStep(
                    workspace_id=run.workspace_id,
                    project_id=run.project_id,
                    run_id=run.id,
                    root_run_id=run.root_run_id,
                    sequence=run.step_number + 1,
                    fence=run.fence,
                    operation_id=operation_id,
                    kind="model",
                    status="dispatched",
                    usage_id=usage.id,
                    request_digest=prepared.request_digest,
                    request={
                        "generation": prepared.request.model_dump(mode="json"),
                        "model_id": str(binding.model.id),
                        "model_version": binding.model.version,
                        "credential_revision": binding.model.credential_revision,
                        "fingerprint": binding.fingerprint,
                        "dependencies": [
                            {k: v for k, v in d.items() if k != "candidate"} for d in dependencies
                        ],
                    },
                )
                self.store.insert_execution_step(current_step)
                run = self._save(run, step_number=current_step.sequence)
            self._event(
                run,
                "step_dispatched",
                sequence=current_step.sequence,
                kind=current_step.kind,
                operation_id=str(current_step.operation_id),
            )
        # No transaction or identity lock is held over a model/provider call.
        result: dict[str, Any] | None = None
        error: str | None = None
        unknown = False
        if current_step.kind == "model":
            assert (
                executor is not None and prepared is not None and current_step.usage_id is not None
            )
            response = None
            try:
                action, response = executor.generate(prepared)
                result = action.model_dump(mode="json", exclude_none=True)
            except WorkflowPlanningError as exc:
                response, error = exc.result, exc.code
                unknown = exc.may_have_been_dispatched and response is None
            except ModelEndpointError as exc:
                error, unknown = exc.code, exc.may_have_been_dispatched
            except Exception:
                error, unknown = "execution_interrupted", True
            settled = self.usage.settle(
                run.workspace_id,
                run.project_id,
                current_step.usage_id,
                response,
                unknown=unknown,
                error_code=error,
            )
            unknown = settled.status == "unknown"
            if settled.error_code == "provider_usage_exceeded_reservation":
                error = settled.error_code
            if unknown:
                error = error or "provider_usage_unverified"
        else:
            try:
                result = self.reference.execute(
                    run.workspace_id,
                    run.project_id,
                    current_step.operation_id,
                    current_step.request,
                )
            except Exception:
                error, unknown = "reference_outcome_unknown", True
        # The checkpoint is durable before publishing candidates, spawning children or
        # registering waits. Late responses may settle costs but cannot use an old lease.
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(run.workspace_id):
            step = self.store.execution_step(run.workspace_id, run.project_id, current_step.id)
            assert step is not None
            if step.status not in {"completed", "failed"}:
                step = self._save(
                    step,
                    status="unknown" if unknown else "failed" if error else "completed",
                    result=result or {},
                    error_code=error,
                    finished_at=utc_now(),
                )
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(run.workspace_id):
            current = self._run(run.workspace_id, run.project_id, run.id)
            try:
                current = self._lease(runner, command)
            except _AUTHORITY_ERRORS:
                if current.status in LIVE:
                    # A response from an expired or superseded lease still settles
                    # usage, but cannot invalidate a newer owner's execution lease.
                    try:
                        self._authority(current)
                    except _AUTHORITY_ERRORS:
                        current = self._finish(current, "stale", "execution_authority_changed")
                return public_run(current)
            if unknown or error:
                return public_run(self._finish(current, "unknown" if unknown else "failed", error))
            return public_run(self._advance_checkpoint(current, step))

    def _advance_checkpoint(
        self, run: NativeExecutionRun, step: NativeExecutionStep
    ) -> NativeExecutionRun:
        try:
            # Partial child admission, task updates and history must all roll back
            # together. The already committed model checkpoint remains available.
            with self.store.transaction(run.workspace_id):
                return self._apply_checkpoint(run, step)
        except (ModelValidationError, *_AUTHORITY_ERRORS):
            return self._finish(run, "failed", "checkpoint_action_invalid")

    def _wait(
        self,
        run: NativeExecutionRun,
        step: NativeExecutionStep,
        kind: Literal["human", "timer", "children"],
        *,
        question: str = "",
        due: datetime | None = None,
    ) -> NativeExecutionRun:
        deadline = min(due or run.deadline_at, run.deadline_at)
        wait = NativeExecutionWait(
            id=uuid5(step.id, "wait"),
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            run_id=run.id,
            correlation_id=self.correlation(run, step.sequence),
            kind=kind,
            question=question,
            deadline_at=deadline,
        )
        self.store.insert_execution_wait(wait)
        current = self._unlease(
            run, status="waiting", applied_step=step.sequence, next_wake_at=deadline
        )
        self._event(
            run,
            "waiting",
            wait_id=str(wait.id),
            wait_kind=kind,
            question=question,
            deadline_at=deadline.isoformat(),
        )
        return self._wake(current)

    def _apply_checkpoint(
        self, run: NativeExecutionRun, step: NativeExecutionStep
    ) -> NativeExecutionRun:
        actor = self._authority(run)
        if step.sequence <= run.applied_step:
            return run
        if step.status != "completed":
            raise InvalidTransitionError("Only a definitive checkpoint can advance execution.")
        if step.kind == "reference":
            self._event(
                run, "reference_completed", operation_id=str(step.operation_id), result=step.result
            )
            if step.result.get("operation") == "delay":
                return self._wait(
                    run, step, "timer", due=datetime.fromisoformat(step.result["ready_at"])
                )
            return self._save(run, applied_step=step.sequence)
        binding = self.models.resolve(actor, run.project_id, run.model_id)
        if (
            binding.fingerprint != step.request.get("fingerprint")
            or binding.model.version != step.request.get("model_version")
            or binding.model.credential_revision != step.request.get("credential_revision")
        ):
            raise InvalidTransitionError("The model authority changed after dispatch.")
        dependencies = self._dependencies(run)
        if dependencies is None or [
            {key: value for key, value in dependency.items() if key != "candidate"}
            for dependency in dependencies
        ] != step.request.get("dependencies", []):
            raise InvalidTransitionError("Dependency input or candidate changed after dispatch.")
        action = WorkflowAction.model_validate_json(json.dumps(step.result), strict=True)
        self._event(
            run, "checkpoint", sequence=step.sequence, action=action.kind, summary=action.summary
        )
        if action.kind == "draft":
            task = self.projects.get_task(actor, run.project_id, run.task_id)
            updated = self.projects.update_task(
                actor,
                run.project_id,
                task.id,
                UpdateNativeTask(
                    expected_version=task.version,
                    idempotency_key=f"execution:{run.id}:candidate:{step.sequence}",
                    title=task.title,
                    description=task.description,
                    assignment=task.assignment,
                    status="in_review",
                ),
            )
            result = self._unlease(
                run,
                status="completed",
                result_text=action.output or "",
                applied_step=step.sequence,
                result_task_version=updated.version,
                finished_at=utc_now(),
            )
            self._event(result, "candidate_ready", task_version=updated.version, accepted=False)
            return result
        if action.kind == "question":
            return self._wait(run, step, "human", question=action.question or "")
        if action.kind == "wait":
            assert action.wait_seconds is not None
            return self._wait(
                run, step, "timer", due=utc_now() + timedelta(seconds=action.wait_seconds)
            )
        if action.kind == "delegate":
            graph = self.store.execution_root_runs(
                run.workspace_id, run.project_id, run.root_run_id
            )
            if (
                len(graph) - 1 + len(action.children) > run.bounds.max_children
                or run.depth >= run.bounds.max_depth
            ):
                raise InvalidTransitionError("The root workflow delegation allowance is exhausted.")
            for index, child in enumerate(action.children):
                identifier = uuid5(step.id, f"child:{index}")
                task = self.projects.create_task(
                    actor,
                    run.project_id,
                    CreateNativeTask(
                        title=child.title,
                        description=child.description
                        + "\n\nDelegation rationale: "
                        + child.rationale,
                        assignment=TaskAssignment(kind="agent", agent_id=child.agent_id),
                        idempotency_key=f"execution:{step.id}:child:{index}",
                    ),
                )
                self._admit(
                    actor,
                    run.project_id,
                    task,
                    agent_id=child.agent_id,
                    parent=run,
                    run_id=identifier,
                )
            return self._wait(run, step, "children")
        if run.step_number >= run.bounds.max_steps:
            raise InvalidTransitionError(
                "No reference-tool step remains in this workflow allowance."
            )
        request = {
            key: step.result[key]
            for key in ("operation", "text", "delay_seconds")
            if key in step.result
        }
        reference = NativeExecutionStep(
            id=uuid5(step.id, "reference"),
            workspace_id=run.workspace_id,
            project_id=run.project_id,
            run_id=run.id,
            root_run_id=run.root_run_id,
            sequence=run.step_number + 1,
            fence=run.fence,
            operation_id=uuid5(step.id, "reference-operation"),
            kind="reference",
            status="prepared",
            request=request,
            request_digest=digest(request),
        )
        self.store.insert_execution_step(reference)
        return self._save(run, applied_step=step.sequence, step_number=reference.sequence)
