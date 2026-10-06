"""Own-calendar creation fenced to a live agent invocation, with durable receipts.

Chat's original-request guard is intentionally unchanged. These receipts live in
Jobs, not chat ActionProposal rows. Provider calls never hold a database lock and
an executing/unknown receipt is never automatically dispatched again.
"""

from collections.abc import Callable
from datetime import UTC
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, HttpUrl

from simon.adapters.google import ConnectedError
from simon.domain.agent_platform import AgentProfile, AgentTeamPlan
from simon.domain.agent_runs import AgentRun
from simon.domain.connected_tools import ActionProposal, CalendarDraft
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.models import ActorContext, Job, JobStatus, StrictModel, utc_now
from simon.services.canonical import digest
from simon.services.connected import ConnectedService
from simon.services.identity import IDENTITY_LOCK, ROLE_SCOPES

CREATE_TOOL = "native.calendar_create_event"
WRITE_SCOPES = frozenset({"jobs:read", "jobs:write", "threads:read", "threads:write"})


class AgentCalendarReceipt(StrictModel):
    action_id: UUID
    run_id: UUID
    task_id: str
    agent_id: str
    connection_id: UUID
    account_email: str
    calendar: CalendarDraft
    status: Literal["executing", "succeeded", "failed", "unknown"] = "executing"
    provider_id: str | None = None
    url: HttpUrl | None = None
    error: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class AgentCalendarStatus(StrictModel):
    action_id: UUID


class AgentCalendarService:
    def __init__(self, connected: ConnectedService) -> None:
        self.connected, self.store = connected, connected.store

    @staticmethod
    def kind(run_id: UUID) -> str:
        return "platform.agent_calendar." + run_id.hex

    def _run(self, actor: ActorContext, run_id: UUID) -> tuple[AgentRun, AgentTeamPlan, Job]:
        required = {"jobs:read", "threads:read"}
        membership = self.connected.identity.membership(actor.actor_id, actor.workspace_id)
        account = self.store.managed_account(actor.actor_id)
        if not required <= (actor.scopes & ROLE_SCOPES[membership.role]) or (
            account is not None and account.disabled
        ):
            raise AuthorizationError("Calendar receipt access is unavailable")
        job = self.store.get_job(run_id)
        if job is None or (job.kind, job.created_by, job.workspace_id) != (
            "platform.run",
            actor.actor_id,
            actor.workspace_id,
        ):
            raise NotFoundError("Agent calendar run not found")
        run = AgentRun.model_validate(job.result or job.input["initial_state"]).model_copy(
            update={"status": job.status},
        )
        saved = self.store.get_job(run.plan_id)
        if saved is None or (saved.kind, saved.created_by, saved.workspace_id) != (
            "platform.plan",
            actor.actor_id,
            actor.workspace_id,
        ):
            raise NotFoundError("Agent calendar plan not found")
        plan = AgentTeamPlan.model_validate(saved.input["plan"])
        if plan.project_id is not None:
            project = self.store.explicit_memory(actor.workspace_id, plan.project_id)
            if (
                not project
                or not project.accepted
                or project.category != "project"
                or (project.scope == "personal" and project.created_by != actor.actor_id)
            ):
                raise NotFoundError("Agent calendar project not found")
        return run, plan, saved

    def _task(
        self,
        actor: ActorContext,
        run_id: UUID,
        agent_id: str,
        invocation_id: UUID,
    ) -> str:
        membership = self.connected.identity.membership(actor.actor_id, actor.workspace_id)
        if not (actor.scopes & ROLE_SCOPES[membership.role]) >= WRITE_SCOPES:
            raise AuthorizationError("Calendar creation permission is unavailable")
        run, plan, saved = self._run(actor, run_id)
        if run.status != JobStatus.RUNNING or run.cancel_requested or run.executor_id is None:
            raise AuthorizationError("Calendar creation requires a live agent run")
        profiles = {
            profile.id: profile
            for value in saved.input["configuration"]["agents"]
            for profile in (AgentProfile.model_validate(value),)
        }
        profile = profiles.get(agent_id)
        if (
            profile is None
            or profile.max_action != "write"
            or profile.privacy == "local_only"
            or (CREATE_TOOL not in profile.tool_ids or not profile.tool_scopes >= WRITE_SCOPES)
        ):
            raise AuthorizationError("This agent cannot create calendar events")
        granted = {
            task.id
            for task in plan.tasks
            if task.agent_id == agent_id and CREATE_TOOL in task.tool_ids
        }
        for task in run.tasks:
            active = task.agent_id == agent_id and task.id in granted and task.status == "running"
            if active and any(
                event.get("event") == "tool_dispatch"
                and event.get("tool_id") == CREATE_TOOL
                and event.get("invocation_id") == str(invocation_id)
                for event in task.events
            ):
                return task.id
        raise AuthorizationError("Calendar creation requires this task's live tool checkpoint")

    @staticmethod
    def _receipt(job: Job) -> AgentCalendarReceipt:
        return AgentCalendarReceipt.model_validate(job.result or job.input["initial_state"])

    def get(self, actor: ActorContext, run_id: UUID, action_id: UUID) -> AgentCalendarReceipt:
        self._run(actor, run_id)
        job = self.store.get_job(action_id)
        if job is None or (job.kind, job.created_by, job.workspace_id) != (
            self.kind(run_id),
            actor.actor_id,
            actor.workspace_id,
        ):
            raise NotFoundError("Calendar action not found in this run")
        return self._receipt(job)

    def list_for_run(self, actor: ActorContext, run_id: UUID) -> tuple[AgentCalendarReceipt, ...]:
        self._run(actor, run_id)
        return tuple(
            self._receipt(job)
            for job in self.store.jobs(
                actor.workspace_id,
                actor.actor_id,
                self.kind(run_id),
                0,
                3,
            )
        )

    def create(
        self,
        actor: ActorContext,
        run_id: UUID,
        agent_id: str,
        invocation_id: UUID,
        draft: CalendarDraft,
        revalidate: Callable[[], ActorContext],
    ) -> AgentCalendarReceipt:
        def checked() -> ActorContext:
            current = revalidate()
            if (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id):
                raise AuthorizationError("Calendar account changed")
            current = current.model_copy(update={"scopes": current.scopes & actor.scopes})
            self._task(current, run_id, agent_id, invocation_id)
            return current

        # Equivalent offsets normalize to one event within this run/account.
        canonical = draft.model_dump(mode="json")
        canonical.update(
            start=draft.start.astimezone(UTC).isoformat(), end=draft.end.astimezone(UTC).isoformat()
        )
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            current = checked()

            def claim() -> dict[str, object]:
                connection = self.connected.connection(current, account=draft.account)
                self.connected.require_google_scope(connection, "calendar_create_event")
                event = {**canonical, "account": str(connection.id)}
                identifier = uuid5(run_id, "agent-calendar:" + digest(event))
                existing = self.store.get_job(identifier)
                if existing is not None:
                    self.get(current, run_id, identifier)
                    return {"action_id": str(identifier), "dispatch": False}
                prior = self.list_for_run(current, run_id)
                if any(item.status in {"executing", "unknown"} for item in prior):
                    raise ValidationError("Resolve this run's uncertain calendar action first")
                if len(prior) >= 3:
                    raise ValidationError("At most three calendar creations per agent run")
                receipt = AgentCalendarReceipt(
                    action_id=identifier,
                    run_id=run_id,
                    task_id=self._task(current, run_id, agent_id, invocation_id),
                    agent_id=agent_id,
                    connection_id=connection.id,
                    account_email=connection.email,
                    calendar=draft.model_copy(update={"account": connection.email}),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                self.store.create_job(
                    Job(
                        id=identifier,
                        kind=self.kind(run_id),
                        workspace_id=current.workspace_id,
                        created_by=current.actor_id,
                        idempotency_key=identifier.hex,
                        input={"initial_state": receipt.model_dump(mode="json")},
                        input_digest=digest(event),
                        status=JobStatus.RUNNING,
                    )
                )
                self.connected.audit.record(
                    actor=current,
                    event_type="agent.calendar_requested",
                    resource_type="action",
                    resource_id=str(identifier),
                    payload={"run_id": str(run_id), "agent_id": agent_id},
                )
                return {"action_id": str(identifier), "dispatch": True}

            claimed, replayed = self.store.execute_once(
                f"agent-calendar:{actor.workspace_id}:{actor.actor_id}:{run_id}",
                str(invocation_id),
                digest({"agent_id": agent_id, "draft": canonical}),
                claim,
            )
        identifier = UUID(str(claimed["action_id"]))
        receipt = self.get(current, run_id, identifier)
        if replayed or not claimed["dispatch"]:
            return receipt
        dispatched = False
        try:
            current = checked()
            connection = self.connected.connection(current, receipt.connection_id)
            self.connected.require_google_scope(connection, "calendar_create_event")
            token = self.connected.access_token(current, connection)
            current = checked()
            live = self.connected.connection(current, receipt.connection_id)
            self.connected.require_google_scope(live, "calendar_create_event")
            if live.email != receipt.account_email:
                raise AuthorizationError("Calendar connection changed")
            action = ActionProposal(
                id=receipt.action_id,
                workspace_id=current.workspace_id,
                actor_id=current.actor_id,
                run_id=run_id,
                connection_id=connection.id,
                account_email=connection.email,
                kind="calendar.create",
                calendar=receipt.calendar,
                immediate=True,
                status="executing",
            )
            dispatched = True
            provider_id, url = self.connected.api.execute(token, action, connection.email)
            if provider_id != action.id.hex:
                raise ConnectedError(
                    "Calendar provider receipt did not match its event",
                    unknown=True,
                )
            receipt = receipt.model_copy(
                update={
                    "status": "succeeded",
                    "provider_id": provider_id,
                    "url": HttpUrl(url) if url else None,
                }
            )
        except Exception as error:
            unknown = dispatched and (not isinstance(error, ConnectedError) or error.unknown)
            receipt = receipt.model_copy(
                update={
                    "status": "unknown" if unknown else "failed",
                    "error": "Calendar outcome unknown; inspect Google Calendar before continuing."
                    if unknown
                    else "Calendar creation failed; check account and run access.",
                }
            )
        receipt = receipt.model_copy(update={"updated_at": utc_now()})
        with self.store.transaction(actor.workspace_id):
            saved = self.store.get_job(identifier)
            assert saved is not None
            status = {
                "succeeded": JobStatus.SUCCEEDED,
                "failed": JobStatus.FAILED,
                "unknown": JobStatus.NEEDS_HUMAN,
                "executing": JobStatus.RUNNING,
            }
            self.store.save_job(
                saved.model_copy(
                    update={
                        "result": receipt.model_dump(mode="json"),
                        "status": status[receipt.status],
                        "updated_at": utc_now(),
                    }
                ),
                saved.version,
            )
            self.connected.audit.record(
                actor=actor,
                event_type="agent.calendar_" + receipt.status,
                resource_type="action",
                resource_id=str(identifier),
                payload={"run_id": str(run_id), "agent_id": agent_id},
            )
        return receipt
