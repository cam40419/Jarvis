"""Persistent run admission, ownership, reservations, and cancellation."""

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4, uuid5

from simon.domain.agent_platform import AgentProfile, AgentTeamPlan, PlanTeamRequest
from simon.domain.agent_runs import AgentRun, ReconcileAgentRun, StartAgentRun, TaskExecution
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Channel, Job, JobStatus, utc_now
from simon.services.agent_platform import AgentPlatformService, stable_configuration
from simon.services.canonical import digest
from simon.services.identity import ROLE_SCOPES

RUN_KIND = "platform.run"
# Both storage implementations serialize this short global reservation transaction.
DISPATCH_LOCK = UUID("fa6cc40a-4760-4afb-a943-6f600e465147")
ActorResolver = Callable[[UUID, UUID], ActorContext]


class AgentRunService:
    def __init__(
        self, platform: AgentPlatformService, *, enabled: bool = False,
        actor_resolver: ActorResolver | None = None,
    ) -> None:
        self.platform, self.store, self.enabled = platform, platform.store, enabled
        self.actor_resolver = actor_resolver or self._resolve_actor

    def _resolve_actor(self, actor_id: UUID, workspace_id: UUID) -> ActorContext:
        account = self.store.managed_account(actor_id)
        if account is not None and account.disabled:
            raise AuthorizationError("Account access has been disabled")
        membership = next((item for item in self.store.memberships(actor_id)
                           if item.household_id == workspace_id), None)
        if membership is None:
            raise AuthorizationError("Workspace membership required")
        return ActorContext(actor_id=actor_id, household_id=workspace_id,
                            channel=Channel.WORKER, scopes=ROLE_SCOPES[membership.role])

    @staticmethod
    def view(job: Job) -> AgentRun:
        state = AgentRun.model_validate(job.result or job.input["initial_state"])
        return state.model_copy(update={"status": job.status, "version": job.version})

    def job(self, identifier: UUID) -> Job:
        job = self.store.get_job(identifier)
        if job is None or job.kind != RUN_KIND:
            raise NotFoundError("Agent run not found")
        return job

    def get(self, actor: ActorContext, identifier: UUID) -> AgentRun:
        self.platform.authorize(actor)
        job = self.job(identifier)
        if (job.created_by, job.household_id) != (actor.actor_id, actor.household_id):
            raise NotFoundError("Agent run not found")
        state = self.view(job)
        self.platform.get(actor, state.plan_id)
        return state

    def list(self, actor: ActorContext) -> tuple[AgentRun, ...]:
        self.platform.authorize(actor)
        result = []
        for job in self.store.jobs(actor.household_id, actor.actor_id, RUN_KIND, 0, 100):
            try:
                result.append(self.get(actor, job.id))
            except NotFoundError:
                continue
        return tuple(result)

    def assert_configuration(self, actor: ActorContext, plan: AgentTeamPlan) -> None:
        current = digest(stable_configuration(self.platform.manifest.model_dump(mode="python")))
        if current != plan.manifest_digest:
            raise InvalidTransitionError("Agent configuration changed; create a new plan")
        self.platform.plan_profiles(actor, plan)

    def assert_team(self, actor: ActorContext, plan: AgentTeamPlan) -> None:
        team = self.platform.resolve_team(actor, plan.team_id, plan.project_id)
        if team.version != plan.team_version or not {
            task.agent_id for task in plan.tasks
        } <= set(team.agent_ids):
            raise InvalidTransitionError("Project team changed; create a new plan")

    def live_actor(self, job: Job) -> ActorContext:
        if not self.enabled:
            raise AuthorizationError("Agent execution is disabled")
        current = self.actor_resolver(job.created_by, job.household_id)
        if (current.actor_id, current.household_id) != (job.created_by, job.household_id):
            raise AuthorizationError("Actor resolver returned a different owner")
        actor = current.model_copy(update={
            "scopes": current.scopes & frozenset(job.input["scopes"]),
        })
        self.platform.authorize(actor, write=True)
        plan = self.platform.get(actor, UUID(job.input["plan_id"]))
        self.assert_configuration(actor, plan)
        self.assert_team(actor, plan)
        return actor

    def _reservations(
        self, actor: ActorContext, plan: AgentTeamPlan, request: PlanTeamRequest,
    ) -> tuple[TaskExecution, ...]:
        profiles = self.platform.plan_profiles(actor, plan)
        endpoints = {item.id: item for item in self.platform.manifest.models}
        specs = {item.id: item for item in request.tasks}
        tasks = []
        for task in plan.tasks:
            if task.model is None or task.blocked_reasons:
                raise InvalidTransitionError("Resolve blocked tasks before starting a run")
            profile = profiles[task.agent_id]
            endpoint, spec = endpoints[task.model.endpoint_id], specs[task.id]
            # Reserve the full permitted loop before siblings can spend independently.
            # This estimates model token costs only; external tool charges are excluded.
            input_rate, output_rate = (
                endpoint.input_cost_per_million_usd, endpoint.output_cost_per_million_usd
            )
            estimate = None
            if endpoint.local or (input_rate is not None and output_rate is not None):
                estimate = (profile.max_steps if task.tool_ids else 1) * (
                    endpoint.context_window_tokens * (input_rate or 0)
                    + min(spec.output_tokens, profile.max_output_tokens) * (output_rate or 0)
                ) / 1_000_000
            if spec.budget_usd is not None and (
                estimate is None or estimate > spec.budget_usd
            ):
                raise ValidationError("Task model budget cannot cover its permitted execution")
            tasks.append(TaskExecution(id=task.id, agent_id=task.agent_id,
                                       environment_id=(task.environment.environment_id
                                                       if task.environment else None),
                                       model_reserved_usd=estimate))
        return tuple(tasks)

    def start(self, actor: ActorContext, plan_id: UUID, request: StartAgentRun) -> AgentRun:
        self.platform.authorize(actor, write=True)
        if not self.enabled:
            raise InvalidTransitionError("Enable agent execution before starting a run")
        plan = self.platform.get(actor, plan_id)
        identifier = uuid5(plan_id, f"run:{request.idempotency_key}")
        fingerprint = digest(request.model_dump(mode="json"))
        with self.store.transaction(actor.household_id):
            old = self.store.get_job(identifier)
            if old is not None:
                if old.input_digest != fingerprint:
                    raise IdempotencyConflictError("Run key was used for different settings")
                return self.get(actor, identifier)
            self.assert_configuration(actor, plan)
            self.assert_team(actor, plan)
            if plan.state != "planned":
                raise InvalidTransitionError("Resolve blocked tasks before starting a run")
            plan_job = self.store.get_job(plan.id)
            assert plan_job is not None
            specifications = PlanTeamRequest.model_validate(plan_job.input["request"])
            tasks = self._reservations(actor, plan, specifications)
            total = (
                sum(task.model_reserved_usd or 0 for task in tasks)
                if all(task.model_reserved_usd is not None for task in tasks) else None
            )
            if request.model_budget_usd is not None and (
                total is None or total > request.model_budget_usd
            ):
                raise ValidationError("Run model budget cannot cover all task reservations")
            state = AgentRun(
                id=identifier, plan_id=plan.id, workspace_id=actor.household_id,
                actor_id=actor.actor_id, tasks=tasks, model_reserved_usd=total,
                model_budget_usd=request.model_budget_usd,
            )
            job = Job(
                id=identifier, household_id=actor.household_id, created_by=actor.actor_id,
                kind=RUN_KIND, idempotency_key=identifier.hex, input_digest=fingerprint,
                input={"plan_id": str(plan.id), "request": request.model_dump(mode="json"),
                       "scopes": sorted(actor.scopes),
                       "initial_state": state.model_dump(mode="json")},
            )
            saved, _created = self.store.create_job(job)
            self.platform.audit.record(
                event_type="platform.run.queued", actor=actor, resource_type=RUN_KIND,
                resource_id=str(identifier), payload={"plan_id": str(plan.id)},
            )
            return self.view(saved)

    def update(
        self, identifier: UUID, change: Callable[[AgentRun], AgentRun],
        *, executor_id: UUID | None = None,
    ) -> AgentRun:
        job = self.job(identifier)
        with self.store.transaction(job.household_id):
            job = self.job(identifier)
            state = self.view(job)
            if executor_id is not None and (
                state.executor_id != executor_id or state.status != JobStatus.RUNNING
            ):
                raise InvalidTransitionError("Dispatcher no longer owns this run")
            updated = change(state)
            saved = self.store.transition_job(
                job.id, job.version, updated.status, updated.model_dump(mode="json"),
            )
            return self.view(saved)

    def task_update(
        self, identifier: UUID, task_id: str, change: Callable[[TaskExecution], TaskExecution],
        *, executor_id: UUID,
    ) -> AgentRun:
        return self.update(identifier, lambda state: state.model_copy(update={
            "tasks": tuple(change(task) if task.id == task_id else task for task in state.tasks),
        }), executor_id=executor_id)

    def cancel(self, actor: ActorContext, identifier: UUID) -> AgentRun:
        self.platform.authorize(actor, write=True)
        self.get(actor, identifier)

        def change(state: AgentRun) -> AgentRun:
            if state.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
                return state
            updates: dict[str, Any] = {"cancel_requested": True}
            if state.status == JobStatus.QUEUED:
                updates.update(status=JobStatus.CANCELLED, finished_at=utc_now(), tasks=tuple(
                    task.model_copy(update={"status": "cancelled"}) for task in state.tasks
                ))
            return state.model_copy(update=updates)

        return self.update(identifier, change)

    def claim(self, identifier: UUID) -> AgentRun | None:
        if not self.enabled:
            return None
        with self.store.transaction(DISPATCH_LOCK):
            job = self.job(identifier)
            if job.status != JobStatus.QUEUED:
                return None
            # No network/provider calls while holding this lock.
            state = self.view(job)
            plan_job = self.store.get_job(state.plan_id)
            assert plan_job is not None
            plan = AgentTeamPlan.model_validate(plan_job.input["plan"])
            slots = min(plan.max_parallel, len(plan.tasks), self.platform.manifest.max_parallel)
            active = self.store.jobs_all(RUN_KIND, 129, status="running")
            used = sum(self.view(item).reserved_slots for item in active)
            if used + slots > self.platform.manifest.max_parallel:
                return None
            def change(current: AgentRun) -> AgentRun:
                if current.status != JobStatus.QUEUED or current.cancel_requested:
                    raise InvalidTransitionError("Run was cancelled or claimed")
                return current.model_copy(update={
                    "status": JobStatus.RUNNING, "execution_started": True,
                    "reserved_slots": slots, "executor_id": uuid4(), "started_at": utc_now(),
                })

            try:
                return self.update(identifier, change)
            except InvalidTransitionError:
                return None

    def reconcile(
        self, actor: ActorContext, identifier: UUID, request: ReconcileAgentRun,
    ) -> AgentRun:
        self.platform.authorize(actor, write=True)
        if "identity:manage" not in actor.scopes:
            raise AuthorizationError("Owner permission required for interrupted-run recovery")
        self.get(actor, identifier)

        return self._reconcile(actor, identifier, request)

    def recover_interrupted(
        self, identifier: UUID, expected_version: int, *, operator_actor_id: UUID,
    ) -> AgentRun:
        """Trusted local CLI recovery after the operator has stopped the old dispatcher.

        This entrypoint intentionally does not depend on the disabled/departed run
        owner's current account. Never expose it as an authenticated user endpoint.
        """
        job = self.job(identifier)
        actor = ActorContext(actor_id=operator_actor_id, household_id=job.household_id,
                             channel=Channel.WORKER, scopes=frozenset({"identity:manage"}))
        return self._reconcile(actor, identifier, ReconcileAgentRun(
            expected_version=expected_version, worker_stopped=True,
        ))

    def _reconcile(
        self, actor: ActorContext, identifier: UUID, request: ReconcileAgentRun,
    ) -> AgentRun:

        def change(state: AgentRun) -> AgentRun:
            if state.version != request.expected_version or state.status != JobStatus.RUNNING:
                raise InvalidTransitionError("Run changed or is not interrupted")
            recovered_tasks = []
            for task in state.tasks:
                updates: dict[str, Any] = {}
                if task.status == "running":
                    updates.update(status="unknown", error_code="interrupted")
                    if task.environment_id and task.environment_lease_id is None:
                        lease = self.platform.environments.get_for_attempt(
                            uuid5(identifier, task.id)
                        )
                        if lease is not None:
                            updates["environment_lease_id"] = lease.id
                elif task.status == "queued":
                    updates["status"] = "cancelled"
                recovered_tasks.append(task.model_copy(update=updates))
            return state.model_copy(update={
                "status": JobStatus.NEEDS_HUMAN, "executor_id": None, "reserved_slots": 0,
                "cancel_requested": True, "finished_at": utc_now(),
                "tasks": tuple(recovered_tasks),
            })

        with self.store.transaction(actor.household_id):
            state = self.update(identifier, change)
            self.platform.audit.record(
                event_type="platform.run.reconciled", actor=actor, resource_type=RUN_KIND,
                resource_id=str(identifier), payload={"worker_stopped": True},
            )
            return state

    def profile(self, actor: ActorContext, identifier: str, plan_id: UUID) -> AgentProfile:
        profiles = self.platform.plan_profiles(actor, self.platform.get(actor, plan_id))
        if identifier not in profiles:
            raise NotFoundError("Agent profile is not assigned to this plan")
        return profiles[identifier]
