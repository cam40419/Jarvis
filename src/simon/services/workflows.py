"""Durable workflow coordinator with receipt-backed home actions."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from simon.domain.errors import (
    AuthorizationError,
    DomainError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.home import DirectHomeControl
from simon.domain.models import ActorContext, Channel, JobStatus, utc_now
from simon.domain.ports import Store
from simon.domain.workflows import (
    TERMINAL_WORKFLOWS,
    ControlWorkflow,
    ControlWorkflowSchedule,
    ControlWorkflowTrigger,
    SaveWorkflow,
    SaveWorkflowSchedule,
    SaveWorkflowTrigger,
    StartWorkflow,
    StepAttempt,
    WorkerHeartbeat,
    WorkflowDefinition,
    WorkflowEvent,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowStepRun,
    WorkflowTrigger,
    WorkflowTriggerSpec,
)
from simon.services.audit import AuditService
from simon.services.canonical import digest
from simon.services.home import HomeService
from simon.services.identity import IDENTITY_LOCK, ROLE_SCOPES, IdentityService
from simon.services.jobs import JobService
from simon.services.printer_status import PrinterStatusService


class WorkflowService:
    lease_seconds = 30

    def __init__(
        self, store: Store, identity: IdentityService, clock: Callable[[], datetime] = utc_now,
        home: HomeService | None = None, printer: PrinterStatusService | None = None,
    ):
        self.store, self.identity, self.clock = store, identity, clock
        self.audit = AuditService(store)
        self.jobs = JobService(store, self.audit)
        self.home = home
        self.printer = printer

    def authorize(self, actor: ActorContext, *, write: bool = False) -> ActorContext:
        membership = self.identity.membership(actor.actor_id, actor.household_id)
        scope = "jobs:write" if write else "jobs:read"
        scopes = ROLE_SCOPES[membership.role] & actor.scopes
        if scope not in scopes:
            raise AuthorizationError("workflow access denied")
        return actor.model_copy(update={"scopes": scopes})

    def owner(self, actor: ActorContext, record: WorkflowDefinition | WorkflowRun | None) -> None:
        if not record or (record.household_id, record.actor_id) != (
            actor.household_id,
            actor.actor_id,
        ):
            raise NotFoundError("workflow not found")

    def definition(
        self, actor: ActorContext, identifier: UUID, version: int | None = None
    ) -> WorkflowDefinition:
        self.authorize(actor)
        record = self.store.workflow(identifier, version)
        self.owner(actor, record)
        assert record
        return record

    def definitions(
        self, actor: ActorContext, offset: int, limit: int
    ) -> tuple[WorkflowDefinition, ...]:
        self.authorize(actor)
        return tuple(self.store.workflows(actor.household_id, actor.actor_id, offset, limit))

    def save(
        self, actor: ActorContext, request: SaveWorkflow, identifier: UUID | None = None
    ) -> WorkflowDefinition:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            for step in request.spec.steps:
                if step.action == "home.set":
                    if "home:control" not in actor.scopes or self.home is None:
                        raise AuthorizationError("scheduled home control is unavailable")
                    self.home.device(actor, step.inputs["device_id"], control=True)
            def operation() -> dict[str, Any]:
                old = self.definition(actor, identifier) if identifier else None
                if (old.version if old else 0) != request.expected_version:
                    raise InvalidTransitionError("workflow changed; reload before saving")
                if (
                    not old
                    and len(self.store.workflows(actor.household_id, actor.actor_id, 0, 100)) >= 100
                ):
                    raise ValidationError("maximum 100 workflows per account")
                record = WorkflowDefinition(
                    id=old.id if old else uuid4(),
                    household_id=actor.household_id,
                    actor_id=actor.actor_id,
                    version=request.expected_version + 1,
                    spec=request.spec,
                    created_at=self.clock(),
                )
                self.store.insert_workflow(record)
                self.advance_triggers(record)
                self.audit.record(
                    event_type="workflow.saved",
                    actor=actor,
                    resource_type="workflow",
                    resource_id=str(record.id),
                    payload={"version": record.version},
                )
                return record.model_dump(mode="json")

            result, _ = self.store.execute_once(
                f"workflow:{actor.household_id}:{actor.actor_id}:{identifier}",
                request.idempotency_key,
                digest(request.model_dump(mode="json")),
                operation,
            )
            return WorkflowDefinition.model_validate(result)

    def delete(
        self, actor: ActorContext, identifier: UUID, expected_version: int
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            definition = self.definition(actor, identifier)
            if definition.version != expected_version:
                raise InvalidTransitionError("workflow changed; reload before deleting")
            active = next(
                (
                    run
                    for run in self.store.workflow_runs(
                        actor.household_id, actor.actor_id, 0, 10_000
                    )
                    if run.definition_id == identifier
                    and run.status not in TERMINAL_WORKFLOWS
                ),
                None,
            )
            if active:
                raise InvalidTransitionError(
                    "cancel the active or scheduled run before deleting this workflow"
                )
            self.store.delete_workflow_schedules(
                identifier, actor.household_id, actor.actor_id
            )
            self.store.delete_workflow_trigger_for_definition(
                identifier, actor.household_id, actor.actor_id
            )
            if not self.store.delete_workflow(
                identifier, actor.household_id, actor.actor_id
            ):
                raise NotFoundError("workflow not found")
            self.audit.record(
                event_type="workflow.deleted",
                actor=actor,
                resource_type="workflow",
                resource_id=str(identifier),
                payload={"version": definition.version},
            )
            return {"id": str(identifier), "deleted": True}

    def advance_triggers(self, definition: WorkflowDefinition) -> None:
        now = self.clock()
        existing = self.store.workflow_triggers_for_definition(definition.id)
        step_ids = {step.id for step in definition.spec.steps}
        for trigger in existing:
            target_valid = trigger.start_step_id is None or trigger.start_step_id in step_ids
            self.store.save_workflow_trigger(
                trigger.model_copy(update={
                    "definition_version": definition.version,
                    "name": definition.spec.name,
                    "enabled": trigger.enabled and target_valid,
                    "next_check_at": trigger.next_check_at if target_valid else None,
                    "version": trigger.version + 1,
                    "updated_at": now,
                }),
                trigger.version,
            )

        # Definitions saved by the first trigger implementation remain compatible.
        spec = definition.spec.trigger
        if spec is not None and not existing:
            self.store.save_workflow_trigger(
                WorkflowTrigger(
                    household_id=definition.household_id,
                    actor_id=definition.actor_id,
                    definition_id=definition.id,
                    definition_version=definition.version,
                    name=definition.spec.name,
                    kind=spec.kind,
                    device_id=spec.device_id,
                    field=spec.field,
                    operator=spec.operator,
                    value=spec.value,
                    next_check_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                0,
            )

    def validate_trigger(self, actor: ActorContext, spec: WorkflowTriggerSpec) -> None:
        if spec.kind == "device.state":
            if "home:read" not in actor.scopes or self.home is None:
                raise AuthorizationError("device state triggers are unavailable")
            assert spec.device_id
            self.home.device(actor, spec.device_id)
        elif self.printer is None:
            raise AuthorizationError("printer triggers are unavailable")

    def create_trigger(
        self, actor: ActorContext, identifier: UUID, request: SaveWorkflowTrigger
    ) -> WorkflowTrigger:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            definition = self.definition(actor, identifier, request.definition_version)
            if request.start_step_id is not None and request.start_step_id not in {
                step.id for step in definition.spec.steps
            }:
                raise ValidationError("trigger start step is not in this workflow version")
            self.validate_trigger(actor, request.trigger)
            existing = self.store.workflow_triggers_for_definition(identifier)
            if len(existing) >= 25:
                raise ValidationError("maximum 25 triggers per workflow")
            predicate = request.trigger
            if any(
                (item.kind, item.device_id, item.field, item.operator, item.value,
                 item.start_step_id)
                == (predicate.kind, predicate.device_id, predicate.field,
                    predicate.operator, predicate.value, request.start_step_id)
                for item in existing
            ):
                raise ValidationError("this trigger is already attached to the workflow")

            def operation() -> dict[str, Any]:
                now = self.clock()
                spec = request.trigger
                trigger = WorkflowTrigger(
                    household_id=actor.household_id,
                    actor_id=actor.actor_id,
                    definition_id=definition.id,
                    definition_version=definition.version,
                    name=definition.spec.name,
                    kind=spec.kind,
                    device_id=spec.device_id,
                    field=spec.field,
                    operator=spec.operator,
                    value=spec.value,
                    start_step_id=request.start_step_id,
                    next_check_at=now,
                    created_at=now,
                    updated_at=now,
                )
                self.store.save_workflow_trigger(trigger, 0)
                self.audit.record(
                    event_type="workflow.trigger_created",
                    actor=actor,
                    resource_type="workflow_trigger",
                    resource_id=str(trigger.id),
                    payload={"definition_id": str(identifier)},
                )
                return trigger.model_dump(mode="json")

            result, _ = self.store.execute_once(
                f"workflow-trigger:{actor.household_id}:{actor.actor_id}:{identifier}",
                request.idempotency_key,
                digest(request.model_dump(mode="json")),
                operation,
            )
            return WorkflowTrigger.model_validate(result)

    def triggers(
        self, actor: ActorContext, offset: int, limit: int
    ) -> tuple[WorkflowTrigger, ...]:
        self.authorize(actor)
        return tuple(self.store.workflow_triggers(
            actor.household_id, actor.actor_id, offset, limit
        ))

    def trigger_owner(
        self, actor: ActorContext, trigger: WorkflowTrigger | None
    ) -> WorkflowTrigger:
        if not trigger or (trigger.household_id, trigger.actor_id) != (
            actor.household_id, actor.actor_id,
        ):
            raise NotFoundError("workflow trigger not found")
        return trigger

    def control_trigger(
        self, actor: ActorContext, identifier: UUID, request: ControlWorkflowTrigger
    ) -> WorkflowTrigger:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            trigger = self.trigger_owner(actor, self.store.workflow_trigger(identifier))
            if trigger.version != request.expected_version:
                raise InvalidTransitionError("trigger changed; reload before controlling it")
            enabled = request.action == "resume"
            if enabled == trigger.enabled:
                raise InvalidTransitionError(f"trigger is already {request.action}d")
            if enabled and trigger.start_step_id is not None:
                definition = self.definition(
                    actor, trigger.definition_id, trigger.definition_version
                )
                if trigger.start_step_id not in {step.id for step in definition.spec.steps}:
                    raise InvalidTransitionError(
                        "trigger start step no longer exists; edit the workflow trigger"
                    )
            if not enabled and any(
                run.definition_id == trigger.definition_id
                and run.status not in TERMINAL_WORKFLOWS
                for run in self.store.workflow_runs(
                    actor.household_id, actor.actor_id, 0, 10_000
                )
            ):
                raise InvalidTransitionError(
                    "cancel or finish active runs before pausing this trigger"
                )
            now = self.clock()
            updated = trigger.model_copy(update={
                "enabled": enabled,
                "next_check_at": now if enabled else None,
                "initialized": False if enabled else trigger.initialized,
                "last_is_printing": None if enabled else trigger.last_is_printing,
                "last_observed_at": None if enabled else trigger.last_observed_at,
                "version": trigger.version + 1,
                "updated_at": now,
            })
            self.store.save_workflow_trigger(updated, trigger.version)
            return updated

    def delete_trigger(
        self, actor: ActorContext, identifier: UUID, expected_version: int
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            trigger = self.trigger_owner(actor, self.store.workflow_trigger(identifier))
            if trigger.version != expected_version:
                raise InvalidTransitionError("trigger changed; reload before deleting it")
            if not self.store.delete_workflow_trigger(
                identifier, actor.household_id, actor.actor_id
            ):
                raise NotFoundError("workflow trigger not found")
            self.audit.record(
                event_type="workflow.trigger_deleted",
                actor=actor,
                resource_type="workflow_trigger",
                resource_id=str(identifier),
                payload={"definition_id": str(trigger.definition_id)},
            )
            return {"id": str(identifier), "deleted": True}

    @staticmethod
    def is_printing(status: dict[str, Any]) -> bool:
        return bool(status.get("online")) and str(status.get("state", "")).upper() in {
            "RUNNING", "PREPARE", "SLICING", "PAUSE",
        }

    @staticmethod
    def trigger_matches(trigger: WorkflowTrigger, observed: bool | float | str) -> bool:
        if trigger.operator == "equals":
            return observed == trigger.value
        if (
            isinstance(observed, (bool, str))
            or isinstance(trigger.value, (bool, str))
        ):
            return False
        if trigger.operator == "above":
            return observed > trigger.value
        return observed < trigger.value

    def trigger_value(
        self, trigger: WorkflowTrigger, actor: ActorContext
    ) -> bool | float | str | None:
        if trigger.kind in {"printer.print_started", "printer.print_finished"}:
            if self.printer is None:
                return None
            printer_status = self.printer.status(actor)
            if not printer_status.get("online"):
                return None
            printing = self.is_printing(printer_status)
            return printing if trigger.kind == "printer.print_started" else not printing
        if self.home is None or not trigger.device_id or not trigger.field:
            return None
        home_status = self.home.read(actor, trigger.device_id)
        value = getattr(home_status, trigger.field, None)
        return value if isinstance(value, (bool, int, float, str)) else None

    def observe_trigger(self, candidate: WorkflowTrigger) -> WorkflowRun | None:
        member = self.identity.membership(candidate.actor_id, candidate.household_id)
        actor = ActorContext(
            actor_id=candidate.actor_id,
            household_id=candidate.household_id,
            channel=Channel.WORKER,
            scopes=ROLE_SCOPES[member.role],
        )
        observed = self.trigger_value(candidate, actor)
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(
            candidate.household_id
        ):
            trigger = self.store.workflow_trigger(candidate.id)
            now = self.clock()
            if not trigger or trigger.version != candidate.version or not trigger.enabled:
                return None
            if observed is None:
                updated = trigger.model_copy(update={
                    "next_check_at": now + timedelta(seconds=15),
                    "version": trigger.version + 1,
                    "updated_at": now,
                })
                self.store.save_workflow_trigger(updated, trigger.version)
                return None
            matched = self.trigger_matches(trigger, observed)
            should_fire = (
                matched
                and trigger.last_is_printing is not True
                and trigger.initialized
            )
            updated = trigger.model_copy(update={
                "initialized": True,
                "last_is_printing": matched,
                "last_observed_at": now,
                "next_check_at": now + timedelta(seconds=10),
                "version": trigger.version + 1,
                "updated_at": now,
            })
            self.store.save_workflow_trigger(updated, trigger.version)
            if not should_fire:
                return None
            return self.start(actor, trigger.definition_id, StartWorkflow(
                definition_version=trigger.definition_version,
                idempotency_key=f"trigger:{trigger.id}:{updated.version}",
            ), start_step_id=trigger.start_step_id)

    def schedule_owner(
        self, actor: ActorContext, schedule: WorkflowSchedule | None
    ) -> WorkflowSchedule:
        if not schedule or (schedule.household_id, schedule.actor_id) != (
            actor.household_id,
            actor.actor_id,
        ):
            raise NotFoundError("workflow schedule not found")
        return schedule

    def schedule(self, actor: ActorContext, identifier: UUID) -> WorkflowSchedule:
        self.authorize(actor)
        return self.schedule_owner(actor, self.store.workflow_schedule(identifier))

    def schedules(
        self, actor: ActorContext, offset: int, limit: int
    ) -> tuple[WorkflowSchedule, ...]:
        self.authorize(actor)
        return tuple(
            self.store.workflow_schedules(
                actor.household_id, actor.actor_id, offset, limit
            )
        )

    @staticmethod
    def next_occurrence(schedule: WorkflowSchedule, after: datetime) -> datetime:
        zone = ZoneInfo(schedule.time_zone)
        days = 1 if schedule.frequency == "daily" else 7
        candidate = (schedule.next_run_at or schedule.start_at).astimezone(zone)
        while candidate.astimezone(UTC) <= after.astimezone(UTC):
            candidate += timedelta(days=days)
        return candidate.astimezone(UTC)

    def create_schedule(
        self, actor: ActorContext, identifier: UUID, request: SaveWorkflowSchedule
    ) -> WorkflowSchedule:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            definition = self.definition(actor, identifier, request.definition_version)
            now = self.clock()
            if not now <= request.start_at <= now + timedelta(days=366):
                raise ValidationError("first occurrence must be within the next year")
            if len(self.store.workflow_schedules(
                actor.household_id, actor.actor_id, 0, 100
            )) >= 100:
                raise ValidationError("maximum 100 recurring schedules per account")

            def operation() -> dict[str, Any]:
                schedule = WorkflowSchedule(
                    household_id=actor.household_id,
                    actor_id=actor.actor_id,
                    definition_id=definition.id,
                    definition_version=definition.version,
                    name=definition.spec.name,
                    frequency=request.frequency,
                    start_at=request.start_at,
                    time_zone=request.time_zone,
                    next_run_at=request.start_at,
                    created_at=now,
                    updated_at=now,
                )
                self.store.save_workflow_schedule(schedule, 0)
                self.audit.record(
                    event_type="workflow.schedule_created",
                    actor=actor,
                    resource_type="workflow_schedule",
                    resource_id=str(schedule.id),
                    payload={"definition_id": str(identifier)},
                )
                return schedule.model_dump(mode="json")

            result, _ = self.store.execute_once(
                f"workflow-schedule:{actor.household_id}:{actor.actor_id}:{identifier}",
                request.idempotency_key,
                digest(request.model_dump(mode="json")),
                operation,
            )
            return WorkflowSchedule.model_validate(result)

    def control_schedule(
        self, actor: ActorContext, identifier: UUID, request: ControlWorkflowSchedule
    ) -> WorkflowSchedule:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            schedule = self.schedule(actor, identifier)
            if schedule.version != request.expected_version:
                raise InvalidTransitionError("schedule changed; reload before controlling it")
            now = self.clock()
            enabled = request.action == "resume"
            if enabled == schedule.enabled:
                raise InvalidTransitionError(f"schedule is already {request.action}d")
            updated = schedule.model_copy(
                update={
                    "enabled": enabled,
                    "next_run_at": self.next_occurrence(schedule, now)
                    if enabled else None,
                    "updated_at": now,
                    "version": schedule.version + 1,
                }
            )
            self.store.save_workflow_schedule(updated, schedule.version)
            return updated

    def delete_schedule(
        self, actor: ActorContext, identifier: UUID, expected_version: int
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            schedule = self.schedule(actor, identifier)
            if schedule.version != expected_version:
                raise InvalidTransitionError("schedule changed; reload before deleting")
            if not self.store.delete_workflow_schedule(
                identifier, actor.household_id, actor.actor_id
            ):
                raise NotFoundError("workflow schedule not found")
            self.audit.record(
                event_type="workflow.schedule_deleted",
                actor=actor,
                resource_type="workflow_schedule",
                resource_id=str(identifier),
                payload={"definition_id": str(schedule.definition_id)},
            )
            return {"id": str(identifier), "deleted": True}

    def fire_schedule(self, candidate: WorkflowSchedule) -> WorkflowRun | None:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(
            candidate.household_id
        ):
            schedule = self.store.workflow_schedule(candidate.id)
            now = self.clock()
            if (
                not schedule
                or schedule.version != candidate.version
                or not schedule.enabled
                or not schedule.next_run_at
                or schedule.next_run_at > now
            ):
                return None
            actor = self.worker_actor(
                WorkflowRun(
                    household_id=schedule.household_id,
                    actor_id=schedule.actor_id,
                    definition_id=schedule.definition_id,
                    definition_version=schedule.definition_version,
                    name=schedule.name,
                    steps=(),
                    created_at=now,
                    start_at=now,
                    expires_at=now + timedelta(days=7),
                    next_wake_at=None,
                )
            )
            due = schedule.next_run_at
            updated = schedule.model_copy(
                update={
                    "next_run_at": self.next_occurrence(schedule, now),
                    "updated_at": now,
                    "version": schedule.version + 1,
                }
            )
            self.store.save_workflow_schedule(updated, schedule.version)
            return self.start(
                actor,
                schedule.definition_id,
                StartWorkflow(
                    definition_version=schedule.definition_version,
                    idempotency_key=f"recurring:{schedule.id}:{due.isoformat()}",
                ),
            )

    def get(self, actor: ActorContext, identifier: UUID) -> WorkflowRun:
        self.authorize(actor)
        run = self.store.workflow_run(identifier)
        self.owner(actor, run)
        assert run
        return run

    def runs(self, actor: ActorContext, offset: int, limit: int) -> tuple[WorkflowRun, ...]:
        self.authorize(actor)
        return tuple(self.store.workflow_runs(actor.household_id, actor.actor_id, offset, limit))

    def events(
        self, actor: ActorContext, identifier: UUID, after: int
    ) -> tuple[WorkflowEvent, ...]:
        self.get(actor, identifier)
        return tuple(self.store.workflow_events(identifier, after))

    def persist(
        self,
        actor: ActorContext,
        run: WorkflowRun,
        event: str,
        expected: int,
        step: str | None = None,
    ) -> WorkflowRun:
        run = run.model_copy(update={"version": expected + 1})
        self.store.save_workflow_run(run, expected)
        self.store.append_workflow_event(
            WorkflowEvent(
                run_id=run.id,
                sequence=run.version,
                type=event,
                step_id=step,
                created_at=self.clock(),
            )
        )
        self.audit.record(
            event_type="workflow." + event,
            actor=actor,
            resource_type="workflow_run",
            resource_id=str(run.id),
            payload={"version": run.version, "step_id": step},
        )
        return run

    def start(
        self, actor: ActorContext, identifier: UUID, request: StartWorkflow,
        *, start_step_id: str | None = None,
    ) -> WorkflowRun:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            definition = self.definition(actor, identifier, request.definition_version)

            def operation() -> dict[str, Any]:
                now = self.clock()
                start = request.start_at or now
                expires = request.expires_at or min(
                    start + timedelta(days=7), now + timedelta(days=30)
                )
                if not now <= start < expires <= now + timedelta(days=30):
                    raise ValidationError(
                        "start must be now or later; expiry must follow it within 30 days"
                    )
                if start_step_id is not None and start_step_id not in {
                    step.id for step in definition.spec.steps
                }:
                    raise ValidationError("trigger start step is not in this workflow version")
                start_index = next(
                    (index for index, step in enumerate(definition.spec.steps)
                     if step.id == start_step_id), 0,
                )
                run_steps = tuple(
                    WorkflowStepRun(
                        step=step,
                        status="succeeded" if index < start_index else "pending",
                        completed_at=now if index < start_index else None,
                        result={"skipped": "trigger_entry"} if index < start_index else None,
                    )
                    for index, step in enumerate(definition.spec.steps)
                )
                run = WorkflowRun(
                    household_id=actor.household_id,
                    actor_id=actor.actor_id,
                    definition_id=definition.id,
                    definition_version=definition.version,
                    name=definition.spec.name,
                    steps=run_steps,
                    created_at=now,
                    start_at=start,
                    expires_at=expires,
                    next_wake_at=start,
                )
                return self.persist(actor, run, "created", 0).model_dump(mode="json")

            result, _ = self.store.execute_once(
                f"workflow-start:{actor.household_id}:{actor.actor_id}:{identifier}",
                request.idempotency_key,
                digest(request.model_dump(mode="json")),
                operation,
            )
            return self.get(actor, UUID(str(result["id"])))

    def worker_actor(self, run: WorkflowRun) -> ActorContext:
        member = self.identity.membership(run.actor_id, run.household_id)
        return self.authorize(
            ActorContext(
                actor_id=run.actor_id,
                household_id=run.household_id,
                channel=Channel.WORKER,
                scopes=ROLE_SCOPES[member.role],
            ),
            write=True,
        )

    def interrupt(
        self, actor: ActorContext, run: WorkflowRun, *, cancel: bool = False
    ) -> WorkflowRun:
        steps = []
        for step in run.steps:
            if step.status == "running":
                attempt = step.attempts[-1]
                job = self.jobs.get(actor, attempt.job_id)
                self.jobs.transition(
                    actor,
                    job.id,
                    expected_version=job.version,
                    status=JobStatus.CANCELLED if cancel else JobStatus.FAILED,
                    error_code="workflow_interrupted",
                )
                attempt = attempt.model_copy(
                    update={"status": "interrupted", "ended_at": self.clock()}
                )
                step = step.model_copy(
                    update={"status": "pending", "attempts": (*step.attempts[:-1], attempt)}
                )
            steps.append(step)
        return run.model_copy(
            update={"steps": tuple(steps), "lease_id": None, "lease_until": None, "worker_id": None}
        )

    def control(
        self, actor: ActorContext, identifier: UUID, request: ControlWorkflow
    ) -> WorkflowRun:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.household_id):
            self.authorize(actor, write=True)
            run = self.get(actor, identifier)
            if run.version != request.expected_version or run.status in TERMINAL_WORKFLOWS:
                raise InvalidTransitionError("run changed or ended; reload before controlling it")
            if request.action == "resume" and run.status not in {"paused", "needs_attention"}:
                raise InvalidTransitionError("only a paused run can resume")
            if request.action == "cancel":
                updated = self.interrupt(actor, run, cancel=True).model_copy(
                    update={"status": "cancelled", "next_wake_at": None}
                )
            elif request.action == "pause":
                updated = run.model_copy(
                    update={"status": "paused", "next_wake_at": run.lease_until}
                )
            else:
                updated = run.model_copy(
                    update={"status": "queued", "error": None, "next_wake_at": self.clock()}
                )
            return self.persist(actor, updated, request.action, run.version)

    def claim(self, identifier: UUID, worker: str) -> WorkflowRun | None:
        candidate = self.store.workflow_run(identifier)
        if not candidate:
            return None
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(candidate.household_id):
            run = self.store.workflow_run(identifier)
            assert run
            now = self.clock()
            if run.status in TERMINAL_WORKFLOWS or not run.next_wake_at or run.next_wake_at > now:
                return None
            original_version = run.version
            # A revoked account can still get a durable failure event without an active session.
            actor = ActorContext(
                actor_id=run.actor_id, household_id=run.household_id, channel=Channel.WORKER
            )
            try:
                actor = self.worker_actor(run)
                if (
                    any(s.step.action == "home.inventory" for s in run.steps)
                    and "home:read" not in actor.scopes
                ):
                    raise AuthorizationError("home access unavailable")
                if (
                    any(s.step.action == "home.set" for s in run.steps)
                    and "home:control" not in actor.scopes
                ):
                    raise AuthorizationError("home control unavailable")
            except DomainError:
                updated = self.interrupt(actor, run).model_copy(
                    update={
                        "status": "needs_attention",
                        "next_wake_at": None,
                        "error": "Account or capability access changed",
                    }
                )
                self.persist(actor, updated, "access_changed", original_version)
                return None
            if now >= run.expires_at:
                updated = self.interrupt(actor, run).model_copy(
                    update={
                        "status": "expired",
                        "next_wake_at": None,
                        "error": "Run deadline passed",
                    }
                )
                self.persist(actor, updated, "expired", original_version)
                return None
            if run.lease_id:
                if run.lease_until and run.lease_until > now:
                    return None
                run = self.interrupt(actor, run)
            if run.status == "paused":
                self.persist(
                    actor, run.model_copy(update={"next_wake_at": None}), "paused", original_version
                )
                return None
            if now < run.start_at:
                self.persist(
                    actor,
                    run.model_copy(update={"next_wake_at": run.start_at}),
                    "scheduled",
                    original_version,
                )
                return None
            steps = list(run.steps)
            completed = {s.step.id: s.completed_at for s in steps if s.status == "succeeded"}
            for index, step in enumerate(steps):
                if step.status == "succeeded":
                    continue
                if (step.step.start_by and now >= step.step.start_by) or len(step.attempts) >= 3:
                    steps[index] = step.model_copy(update={"status": "failed"})
                    attention = step.step.action == "home.set" and bool(step.attempts)
                    updated = run.model_copy(
                        update={
                            "steps": tuple(steps),
                            "status": "needs_attention" if attention else "failed",
                            "next_wake_at": None,
                            "error": "Check the device command receipt before retrying"
                            if attention else "Step deadline or retry limit reached",
                        }
                    )
                    self.persist(
                        actor, updated, "needs_attention" if attention else "failed",
                        original_version, step.step.id,
                    )
                    return None
                if not set(step.step.depends_on) <= completed.keys():
                    continue
                base = max(
                    (completed[d] or run.start_at for d in step.step.depends_on),
                    default=run.start_at,
                )
                due = step.due_at or max(
                    base + timedelta(seconds=step.step.delay_seconds), step.step.not_before or base
                )
                step = step.model_copy(update={"status": "waiting", "due_at": due})
                steps[index] = step
                if due > now:
                    continue
                if step.step.kind == "wait":
                    steps[index] = step.model_copy(
                        update={"status": "succeeded", "completed_at": now}
                    )
                    self.persist(
                        actor,
                        run.model_copy(
                            update={"steps": tuple(steps), "status": "queued", "next_wake_at": now}
                        ),
                        "wait_completed",
                        original_version,
                        step.step.id,
                    )
                    return None
                if step.step.kind == "condition":
                    trigger = next(
                        (
                            item
                            for item in self.store.workflow_triggers_for_definition(
                                run.definition_id
                            )
                            if item.kind == "printer.print_started"
                        ),
                        None,
                    )
                    matched = (
                        step.step.condition == "printer.not_printing"
                        and trigger is not None
                        and trigger.last_is_printing is False
                    )
                    if matched:
                        steps[index] = step.model_copy(
                            update={"status": "succeeded", "completed_at": now}
                        )
                        self.persist(
                            actor,
                            run.model_copy(update={
                                "steps": tuple(steps), "status": "queued", "next_wake_at": now,
                            }),
                            "condition_met",
                            original_version,
                            step.step.id,
                        )
                    else:
                        steps[index] = step.model_copy(
                            update={"status": "waiting", "due_at": now + timedelta(seconds=15)}
                        )
                        self.persist(
                            actor,
                            run.model_copy(update={
                                "steps": tuple(steps),
                                "status": "waiting",
                                "next_wake_at": now + timedelta(seconds=15),
                            }),
                            "condition_waiting",
                            original_version,
                            step.step.id,
                        )
                    return None
                job, _ = self.jobs.submit(
                    actor,
                    kind="workflow.action",
                    input={
                        "action": step.step.action,
                        "inputs": step.step.inputs,
                        "run_id": str(run.id),
                        "step_id": step.step.id,
                    },
                    idempotency_key=f"workflow:{run.id}:{step.step.id}:{len(step.attempts) + 1}",
                )
                self.jobs.transition(
                    actor, job.id, expected_version=job.version, status=JobStatus.RUNNING
                )
                attempt = StepAttempt(job_id=job.id, started_at=now)
                steps[index] = step.model_copy(
                    update={"status": "running", "attempts": (*step.attempts, attempt)}
                )
                until = min(now + timedelta(seconds=self.lease_seconds), run.expires_at)
                return self.persist(
                    actor,
                    run.model_copy(
                        update={
                            "steps": tuple(steps),
                            "status": "running",
                            "lease_id": attempt.id,
                            "lease_until": until,
                            "worker_id": worker,
                            "next_wake_at": until,
                        }
                    ),
                    "step_started",
                    original_version,
                    step.step.id,
                )
            if all(s.status == "succeeded" for s in steps):
                state, wake = "succeeded", None
            else:
                state = "waiting"
                wake = min(
                    [
                        run.expires_at,
                        *(s.due_at for s in steps if s.status == "waiting" and s.due_at),
                        *(
                            s.step.start_by
                            for s in steps
                            if s.status != "succeeded" and s.step.start_by
                        ),
                    ]
                )
            self.persist(
                actor,
                run.model_copy(
                    update={"steps": tuple(steps), "status": state, "next_wake_at": wake}
                ),
                state,
                original_version,
            )
            return None

    def execute(self, claim: WorkflowRun) -> dict[str, Any]:
        # Device writes use the durable home command receipt and a stable step key.
        actor = self.worker_actor(claim)
        step = next(s for s in claim.steps if s.status == "running")
        if step.step.action == "system.echo":
            return {"value": step.step.inputs}
        if step.step.action == "home.inventory" and "home:read" in actor.scopes:
            return {
                "source": "saved_inventory",
                "live_status": False,
                "devices": [
                    {"id": d.id, "name": d.name, "room": d.room}
                    for d in self.store.home_devices(actor.household_id)[:200]
                ],
            }
        if step.step.action == "home.set" and "home:control" in actor.scopes and self.home:
            command = self.home.direct_control(
                actor,
                step.step.inputs["device_id"],
                DirectHomeControl.model_validate({
                    **{k: v for k, v in step.step.inputs.items() if k != "device_id"},
                    "idempotency_key": f"workflow:{claim.id}:{step.step.id}",
                }),
                lambda: self.worker_actor(claim),
            )
            if command.status == "succeeded" and not command.verified:
                command = self.home.verify_command(actor, command.id)
            if command.status != "succeeded" or not command.verified:
                raise RuntimeError("scheduled home command needs attention")
            return {"command_id": str(command.id), "verified": command.verified,
                    "status": command.status}
        raise AuthorizationError("unsupported workflow action")

    def finish(
        self, claim: WorkflowRun, result: dict[str, Any] | None, *, failed: bool = False
    ) -> bool:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(claim.household_id):
            run = self.store.workflow_run(claim.id)
            if (
                not run
                or run.lease_id != claim.lease_id
                or not run.lease_until
                or run.lease_until <= self.clock()
            ):
                return False
            try:
                actor = self.worker_actor(run)
            except DomainError:
                actor = ActorContext(
                    actor_id=run.actor_id, household_id=run.household_id, channel=Channel.WORKER
                )
                updated = self.interrupt(actor, run).model_copy(
                    update={
                        "status": "needs_attention",
                        "next_wake_at": None,
                        "error": "Account or capability access changed",
                    }
                )
                self.persist(actor, updated, "access_changed", run.version)
                return False
            if (
                any(s.step.action == "home.inventory" for s in run.steps)
                and "home:read" not in actor.scopes
            ):
                updated = self.interrupt(actor, run).model_copy(
                    update={
                        "status": "needs_attention",
                        "next_wake_at": None,
                        "error": "Home access changed",
                    }
                )
                self.persist(actor, updated, "access_changed", run.version)
                return False
            if (
                any(s.step.action == "home.set" for s in run.steps)
                and "home:control" not in actor.scopes
            ):
                updated = self.interrupt(actor, run).model_copy(
                    update={"status": "needs_attention", "next_wake_at": None,
                            "error": "Home control access changed"}
                )
                self.persist(actor, updated, "access_changed", run.version)
                return False
            step_id = next(s.step.id for s in run.steps if s.status == "running")
            steps = []
            for step in run.steps:
                if step.status == "running":
                    attempt = step.attempts[-1]
                    job = self.jobs.get(actor, attempt.job_id)
                    self.jobs.transition(
                        actor,
                        job.id,
                        expected_version=job.version,
                        status=JobStatus.FAILED if failed else JobStatus.SUCCEEDED,
                        result=None if failed else result,
                        error_code="workflow_action_failed" if failed else None,
                    )
                    attempt = attempt.model_copy(
                        update={
                            "status": "failed" if failed else "succeeded",
                            "ended_at": self.clock(),
                        }
                    )
                    step = step.model_copy(
                        update={
                            "status": "pending" if failed else "succeeded",
                            "completed_at": None if failed else self.clock(),
                            "attempts": (*step.attempts[:-1], attempt),
                            "result": None if failed else result,
                        }
                    )
                steps.append(step)
            paused = run.status == "paused"
            updated = run.model_copy(
                update={
                    "steps": tuple(steps),
                    "status": "paused" if paused else "queued",
                    "lease_id": None,
                    "lease_until": None,
                    "worker_id": None,
                    "next_wake_at": None
                    if paused
                    else self.clock() + timedelta(seconds=5 if failed else 0),
                }
            )
            self.persist(
                actor, updated, "step_failed" if failed else "step_completed", run.version, step_id
            )
            return True

    def tick(self, worker: str, limit: int = 20) -> int:
        self.store.worker_heartbeat(WorkerHeartbeat(id=worker, seen_at=self.clock()))
        executed = 0
        for trigger in self.store.due_workflow_triggers(self.clock(), limit):
            try:
                self.observe_trigger(trigger)
            except DomainError:
                with self.store.transaction(trigger.household_id):
                    current = self.store.workflow_trigger(trigger.id)
                    if current and current.version == trigger.version and current.enabled:
                        now = self.clock()
                        self.store.save_workflow_trigger(
                            current.model_copy(update={
                                "next_check_at": now + timedelta(minutes=1),
                                "version": current.version + 1,
                                "updated_at": now,
                            }),
                            current.version,
                        )
        for schedule in self.store.due_workflow_schedules(self.clock(), limit):
            try:
                self.fire_schedule(schedule)
            except DomainError:
                continue
        for run in self.store.due_workflows(self.clock(), limit):
            claim = self.claim(run.id, worker)
            if claim:
                try:
                    result = self.execute(claim)
                except Exception:
                    self.finish(claim, None, failed=True)
                else:
                    self.finish(claim, result)
                executed += 1
        return executed

    def health(self, actor: ActorContext) -> dict[str, Any]:
        self.authorize(actor)
        latest = next(iter(self.store.workflow_workers()), None)
        return {
            "worker_online": bool(latest and self.clock() - latest.seen_at < timedelta(seconds=45)),
            "last_seen_at": latest.seen_at if latest else None,
            "actions": ["system.echo", "home.inventory", *(["home.set"] if self.home else [])],
            "recurring_schedules": True,
            "event_triggers": self.printer is not None or self.home is not None,
            "device_control": self.home is not None,
        }
