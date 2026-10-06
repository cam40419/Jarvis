"""Owner-scoped durable follow-ups, attention waits and bounded calendar wakeups."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from simon.domain.errors import (
    DomainError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Job, JobStatus
from simon.domain.project_continuity import (
    CreateProjectSchedule,
    CreateProjectWait,
    ProjectScheduleSpec,
    QueueProjectRequest,
    ReplyProjectWait,
    SetProjectSchedule,
)
from simon.domain.project_work import ProjectCycle
from simon.services.canonical import digest
from simon.services.project_calendar import next_occurrence
from simon.services.project_work import ProjectWorkService

REQUEST_KIND = "platform.project_request"
WAIT_KIND = "platform.project_wait"
SCHEDULE_KIND = "platform.project_schedule"


class ProjectContinuityService:
    def __init__(self, work: ProjectWorkService) -> None:
        self.work, self.store = work, work.store
        work.wait_recorder = self.record_cycle_wait

    def _list(self, actor: ActorContext, project_id: UUID, kind: str) -> list[Job]:
        self.work.authorize(actor)
        self.work.project_resolver(actor, project_id)
        return [
            row
            for row in self.store.jobs(actor.workspace_id, actor.actor_id, kind, 0, 10000)
            if row.input.get("project_id") == str(project_id)
        ]

    def _owned(self, actor: ActorContext, project_id: UUID, identifier: UUID, kind: str) -> Job:
        self.work.authorize(actor)
        self.work.project_resolver(actor, project_id)
        job = self.store.get_job(identifier)
        if job is None or (
            job.kind,
            job.workspace_id,
            job.created_by,
            job.input.get("project_id"),
        ) != (
            kind,
            actor.workspace_id,
            actor.actor_id,
            str(project_id),
        ):
            raise NotFoundError("Project continuity record not found")
        return job

    @staticmethod
    def view(job: Job) -> dict[str, Any]:
        return {
            **(job.result or {}),
            "id": str(job.id),
            "version": job.version,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }

    def _save(self, job: Job, data: dict[str, Any], status: JobStatus) -> Job:
        return self.store.save_job(
            job.model_copy(
                update={
                    "result": data,
                    "status": status,
                    "input": {
                        **job.input,
                        "rank": int(datetime.fromisoformat(data["next_run_at"]).timestamp()),
                    }
                    if job.kind == SCHEDULE_KIND and data.get("next_run_at")
                    else job.input,
                    "updated_at": self.work.clock(),
                }
            ),
            job.version,
        )

    def _create(
        self,
        actor: ActorContext,
        project_id: UUID,
        kind: str,
        key: str,
        payload: dict[str, Any],
        initial: dict[str, Any],
    ) -> Job:
        self.work.authorize(actor, write=True)
        self.work.project_resolver(actor, project_id)
        self.work._key(key)
        identifier = uuid5(self.work.identifier(actor, project_id), kind + ":" + key)
        job, _ = self.store.create_job(
            Job(
                id=identifier,
                workspace_id=actor.workspace_id,
                created_by=actor.actor_id,
                kind=kind,
                idempotency_key=identifier.hex,
                input={
                    "project_id": str(project_id),
                    "scopes": sorted(actor.scopes),
                    "rank": int(datetime.fromisoformat(initial["next_run_at"]).timestamp())
                    if kind == SCHEDULE_KIND and initial.get("next_run_at")
                    else int(self.work.clock().timestamp()),
                    "request": payload,
                },
                input_digest=digest(payload),
                result=initial,
                status=JobStatus.QUEUED,
                created_at=self.work.clock(),
                updated_at=self.work.clock(),
            )
        )
        return job

    def _target(self, actor: ActorContext, project_id: UUID, agent_id: str | None) -> None:
        state = self.work.get(actor, project_id)
        if state.team is None:
            raise ValidationError("Choose a project team before queuing work")
        if agent_id is not None and agent_id not in state.team.agent_ids:
            raise ValidationError("The selected agent must belong to this project team")

    def queue(
        self,
        actor: ActorContext,
        project_id: UUID,
        body: QueueProjectRequest,
        *,
        budget: float | None = None,
        bounded: bool = False,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction(actor.workspace_id):
            self._target(actor, project_id, body.agent_id)
            rows = self._list(actor, project_id, REQUEST_KIND)
            identifier = uuid5(
                self.work.identifier(actor, project_id), REQUEST_KIND + ":" + body.idempotency_key
            )
            if sum(row.status == JobStatus.QUEUED for row in rows) >= 100 and not any(
                row.id == identifier for row in rows
            ):
                raise ValidationError("This project already has 100 queued requests")
            payload = {
                **body.model_dump(mode="json"),
                "budget": budget,
                "bounded": bounded,
                "source": source,
            }
            job = self._create(
                actor,
                project_id,
                REQUEST_KIND,
                body.idempotency_key,
                payload,
                {
                    "instruction": body.instruction,
                    "agent_id": body.agent_id,
                    "status": "queued",
                    "cycle_id": None,
                    "source": source,
                    "last_error": None,
                },
            )
            return self.view(job)

    def cancel_request(
        self, actor: ActorContext, project_id: UUID, identifier: UUID, expected_version: int
    ) -> dict[str, Any]:
        self.work.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            job = self._owned(actor, project_id, identifier, REQUEST_KIND)
            if job.version != expected_version or job.status not in {
                JobStatus.QUEUED,
                JobStatus.WAITING,
            }:
                raise InvalidTransitionError("Request changed or has already started")
            return self.view(
                self._save(job, {**(job.result or {}), "status": "cancelled"}, JobStatus.CANCELLED)
            )

    def retry_request(
        self, actor: ActorContext, project_id: UUID, identifier: UUID, expected_version: int
    ) -> dict[str, Any]:
        self.work.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            job = self._owned(actor, project_id, identifier, REQUEST_KIND)
            if job.version != expected_version or job.status != JobStatus.WAITING:
                raise InvalidTransitionError("Only a blocked queued request can be retried")
            # Retry retains the original scoped authorization; it does not adopt
            # permissions from arbitrary source content or silently expand a grant.
            current = self.work.live_actor(job)
            self._target(current, project_id, job.input["request"].get("agent_id"))
            return self.view(
                self._save(
                    job,
                    {**(job.result or {}), "status": "queued", "last_error": None},
                    JobStatus.QUEUED,
                )
            )

    def create_wait(
        self,
        actor: ActorContext,
        project_id: UUID,
        body: CreateProjectWait,
        *,
        cycle_id: UUID | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction(actor.workspace_id):
            self._target(actor, project_id, body.agent_id)
            job = self._create(
                actor,
                project_id,
                WAIT_KIND,
                body.idempotency_key,
                {**body.model_dump(mode="json"), "cycle_id": str(cycle_id) if cycle_id else None},
                {
                    "question": body.question,
                    "instruction": body.instruction,
                    "agent_id": body.agent_id,
                    "status": "waiting",
                    "reply": None,
                    "due_at": body.due_at.isoformat() if body.due_at else None,
                    "cycle_id": str(cycle_id) if cycle_id else None,
                    "request_id": None,
                },
            )
            return self.view(job)

    def record_cycle_wait(self, actor: ActorContext, project_id: UUID, cycle: ProjectCycle) -> None:
        self.create_wait(
            actor,
            project_id,
            CreateProjectWait(
                instruction=cycle.instruction,
                question=cycle.error or "The project lead needs your input.",
                agent_id=cycle.target_agent_id,
                idempotency_key="cycle-wait:" + cycle.id.hex,
            ),
            cycle_id=cycle.id,
        )

    def cancel_wait(
        self, actor: ActorContext, project_id: UUID, identifier: UUID, expected_version: int
    ) -> dict[str, Any]:
        self.work.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            job = self._owned(actor, project_id, identifier, WAIT_KIND)
            data = job.result or {}
            if job.version != expected_version or data.get("status") != "waiting":
                raise InvalidTransitionError("This question changed or has already been answered")
            return self.view(self._save(job, {**data, "status": "cancelled"}, JobStatus.CANCELLED))

    def reply(
        self, actor: ActorContext, project_id: UUID, identifier: UUID, body: ReplyProjectWait
    ) -> dict[str, Any]:
        self.work.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            # Cached replies must recheck current project visibility and the exact
            # owner/project boundary before returning any retained response.
            self._owned(actor, project_id, identifier, WAIT_KIND)

            def operation() -> dict[str, Any]:
                job = self._owned(actor, project_id, identifier, WAIT_KIND)
                data = job.result or {}
                if job.version != body.expected_version or data.get("status") != "waiting":
                    raise InvalidTransitionError("This question has changed or already has a reply")
                # Keep the exact original request and reply in their records. Bound only the
                # compiled continuation prompt, explicitly retaining the original wait ID.
                instruction = str(data["instruction"])
                suffix = "\n\nReply to saved question " + str(identifier) + ":\n" + body.message
                if len(instruction) + len(suffix) > 16000:
                    raise ValidationError(
                        "The request and reply exceed 16000 characters; shorten the reply"
                    )
                request = self.queue(
                    actor,
                    project_id,
                    QueueProjectRequest(
                        instruction=instruction + suffix,
                        agent_id=data.get("agent_id"),
                        idempotency_key="wait-reply:" + identifier.hex,
                    ),
                    source={"wait_id": str(identifier), "cycle_id": data.get("cycle_id")},
                )
                saved = self._save(
                    job,
                    {
                        **data,
                        "status": "replied",
                        "reply": body.message,
                        "request_id": request["id"],
                    },
                    JobStatus.SUCCEEDED,
                )
                return self.view(saved)

            result, _ = self.store.execute_once(
                f"project-wait-reply:{actor.workspace_id}:{actor.actor_id}:{identifier}",
                body.idempotency_key,
                digest(body.model_dump(mode="json")),
                operation,
            )
            return result

    def schedule(
        self, actor: ActorContext, project_id: UUID, body: CreateProjectSchedule
    ) -> dict[str, Any]:
        with self.store.transaction(actor.workspace_id):
            self.work.authorize(actor, write=True)
            self._target(actor, project_id, body.agent_id)
            identifier = uuid5(
                self.work.identifier(actor, project_id), SCHEDULE_KIND + ":" + body.idempotency_key
            )
            existing = self.store.get_job(identifier)
            if existing is not None:
                existing = self._owned(actor, project_id, identifier, SCHEDULE_KIND)
                if existing.input_digest != digest(body.model_dump(mode="json")):
                    raise IdempotencyConflictError(
                        "Schedule key already belongs to different content"
                    )
                return self.view(existing)
            due = next_occurrence(body, self.work.clock())
            if due is None:
                raise ValidationError("Choose a future time for a one-time schedule")
            job = self._create(
                actor,
                project_id,
                SCHEDULE_KIND,
                body.idempotency_key,
                body.model_dump(mode="json"),
                {
                    **body.model_dump(mode="json", exclude={"idempotency_key"}),
                    "enabled": True,
                    "runs_used": 0,
                    "next_run_at": due.isoformat(),
                    "last_error": None,
                },
            )
            return self.view(job)

    def set_schedule(
        self, actor: ActorContext, project_id: UUID, identifier: UUID, body: SetProjectSchedule
    ) -> dict[str, Any]:
        self.work.authorize(actor, write=True)
        with self.store.transaction(actor.workspace_id):
            job = self._owned(actor, project_id, identifier, SCHEDULE_KIND)
            if job.version != body.expected_version:
                raise InvalidTransitionError("The schedule changed; reload before editing")
            data = job.result or {}
            spec = self._schedule_spec(data)
            if body.enabled and int(data["runs_used"]) >= spec.max_runs:
                raise InvalidTransitionError("This schedule has used its authorized run allowance")
            due = next_occurrence(spec, self.work.clock()) if body.enabled else None
            if body.enabled and due is None:
                raise InvalidTransitionError("The one-time occurrence has passed")
            return self.view(
                self._save(
                    job,
                    {
                        **data,
                        "enabled": body.enabled,
                        "next_run_at": due.isoformat() if due else None,
                        "last_error": None,
                    },
                    JobStatus.QUEUED if body.enabled else JobStatus.WAITING,
                )
            )

    @staticmethod
    def _schedule_spec(data: dict[str, Any]) -> ProjectScheduleSpec:
        return ProjectScheduleSpec.model_validate(
            {key: data[key] for key in ProjectScheduleSpec.model_fields}
        )

    def snapshot(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        def visible(kind: str) -> list[dict[str, Any]]:
            rows = self._list(actor, project_id, kind)
            rows.sort(
                key=lambda row: (
                    row.status not in {JobStatus.QUEUED, JobStatus.WAITING},
                    -row.created_at.timestamp(),
                )
            )
            return [self.view(row) for row in rows[:100]]

        return {
            "requests": visible(REQUEST_KIND),
            "waits": visible(WAIT_KIND),
            "schedules": visible(SCHEDULE_KIND),
            "execution_policy": self.work.get(actor, project_id).autonomy.execution_policy,
        }

    def tick(self, limit: int = 20) -> int:
        changed = 0
        # Calendar occurrence and queue insertion commit together. One missed
        # occurrence is retained after downtime; the next cursor is in the future.
        scheduled = 0
        for candidate in self.store.jobs_all(SCHEDULE_KIND, 10000, "queued"):
            if scheduled >= limit:
                break
            with self.store.transaction(candidate.workspace_id):
                job = self.store.get_job(candidate.id)
                if (
                    job is None
                    or job.version != candidate.version
                    or job.status != JobStatus.QUEUED
                ):
                    continue
                data = job.result or {}
                due = datetime.fromisoformat(data["next_run_at"])
                if due > self.work.clock():
                    continue
                try:
                    actor = self.work.live_actor(job)
                    state = self.work.get(actor, UUID(job.input["project_id"]))
                    if state.autonomy.paused or state.blocked_reasons:
                        continue
                    spec = self._schedule_spec(data)
                    self._enqueue_occurrence(actor, job, spec, due)
                except DomainError as error:
                    self._save(
                        job, {**data, "enabled": False, "last_error": str(error)}, JobStatus.WAITING
                    )
                changed += 1
                scheduled += 1
        claimed = 0
        for candidate in self.store.jobs_all(REQUEST_KIND, 10000, "queued"):
            if claimed >= limit:
                break
            with self.store.transaction(candidate.workspace_id):
                job = self.store.get_job(candidate.id)
                if (
                    job is None
                    or job.version != candidate.version
                    or job.status != JobStatus.QUEUED
                ):
                    continue
                try:
                    actor = self.work.live_actor(job)
                    project_id = UUID(job.input["project_id"])
                    state = self.work.get(actor, project_id)
                    if state.active_cycle or state.autonomy.paused or state.blocked_reasons:
                        continue
                    self._claim_request(actor, project_id, job, state.version)
                except DomainError as error:
                    self._save(
                        job,
                        {**(job.result or {}), "status": "blocked", "last_error": str(error)},
                        JobStatus.WAITING,
                    )
                changed += 1
                claimed += 1
        return changed

    def _enqueue_occurrence(
        self, actor: ActorContext, job: Job, spec: ProjectScheduleSpec, due: datetime
    ) -> None:
        # A savepoint also rolls back queue insertion when an expected storage
        # conflict is caught by tick and represented as a visible schedule error.
        with self.store.transaction(actor.workspace_id):
            self.queue(
                actor,
                UUID(job.input["project_id"]),
                QueueProjectRequest(
                    instruction=spec.instruction,
                    agent_id=spec.agent_id,
                    idempotency_key=f"calendar:{job.id}:{due.isoformat()}",
                ),
                budget=spec.model_budget_usd,
                bounded=True,
                source={"schedule_id": str(job.id), "scheduled_for": due.isoformat()},
            )
            data = job.result or {}
            used = int(data["runs_used"]) + 1
            upcoming = next_occurrence(spec, self.work.clock()) if used < spec.max_runs else None
            self._save(
                job,
                {
                    **data,
                    "runs_used": used,
                    "enabled": upcoming is not None,
                    "next_run_at": upcoming.isoformat() if upcoming else None,
                    "last_error": None,
                },
                JobStatus.QUEUED if upcoming else JobStatus.SUCCEEDED,
            )

    def _claim_request(
        self, actor: ActorContext, project_id: UUID, job: Job, expected_version: int
    ) -> None:
        with self.store.transaction(actor.workspace_id):
            request = job.input["request"]
            state = self.work.request_cycle(
                actor,
                project_id,
                request["instruction"],
                "queued:" + job.id.hex,
                expected_version=expected_version,
                target_agent_id=request.get("agent_id"),
                request_id=job.id,
                bounded_execution=bool(request.get("bounded")),
                model_budget_usd=request.get("budget"),
            )
            assert state.active_cycle
            self._save(
                job,
                {
                    **(job.result or {}),
                    "status": "started",
                    "cycle_id": str(state.active_cycle.id),
                    "last_error": None,
                },
                JobStatus.SUCCEEDED,
            )
