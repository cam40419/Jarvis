"""Bounded project history over durable plans and runs, including older cycles."""

import base64
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime
from pydantic import ValidationError as PydanticError

from simon.domain.errors import NotFoundError, ValidationError
from simon.domain.models import ActorContext, JobStatus, StrictModel
from simon.services.agent_runs import AgentRunService
from simon.services.project_work import ProjectWorkService


class ProjectRunTaskSummary(StrictModel):
    id: str
    agent_id: str
    status: str
    artifact_count: int


class ProjectRunSummary(StrictModel):
    id: UUID
    plan_id: UUID
    phase: Literal["planning", "execution", "run"]
    cycle_id: UUID | None
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    task_count: int
    artifact_count: int
    tasks: tuple[ProjectRunTaskSummary, ...]
    run_url: str
    plan_url: str


class ProjectRunPage(StrictModel):
    project_id: UUID
    items: tuple[ProjectRunSummary, ...]
    next_cursor: str | None


class _Cursor(StrictModel):
    version: Literal[1] = 1
    project_id: UUID
    actor_id: UUID
    workspace_id: UUID
    created_at: AwareDatetime
    id: UUID


class ProjectHistoryService:
    def __init__(self, work: ProjectWorkService, runs: AgentRunService) -> None:
        self.work, self.runs, self.store = work, runs, runs.store

    @staticmethod
    def _cursor(
        actor: ActorContext,
        project_id: UUID,
        cursor: str | None,
    ) -> tuple[datetime, UUID] | None:
        if cursor is None:
            return None
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", cursor):
                raise ValueError("Invalid encoding")
            value = _Cursor.model_validate_json(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)),
            )
            if (value.project_id, value.actor_id, value.workspace_id) != (
                project_id,
                actor.actor_id,
                actor.household_id,
            ):
                raise ValueError("Cursor belongs to another project or account")
        except (ValueError, PydanticError) as error:
            raise ValidationError("Invalid project history cursor") from error
        return value.created_at, value.id

    def list(
        self,
        actor: ActorContext,
        project_id: UUID,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ProjectRunPage:
        self.work.get(actor, project_id)
        if not 1 <= limit <= 50:
            raise ValidationError("Project history page size must be between 1 and 50")
        before = self._cursor(actor, project_id, cursor)
        rows = self.store.project_run_jobs(
            actor.household_id,
            actor.actor_id,
            project_id,
            before,
            limit + 1,
        )
        items = []
        for job in rows[:limit]:
            try:
                # Reuse all current run, plan, context and project authorization gates.
                run = self.runs.get(actor, job.id)
            except NotFoundError:
                continue
            plan = self.store.get_job(run.plan_id)
            if plan is None or plan.input.get("plan", {}).get("project_id") != str(project_id):
                continue
            request_key = plan.input.get("request", {}).get("idempotency_key", "")
            match = re.fullmatch(r"project:([0-9a-fA-F-]{36}):(planning|execution)", request_key)
            cycle_id = None
            phase: Literal["planning", "execution", "run"] = "run"
            if match:
                try:
                    cycle_id = UUID(match[1])
                except ValueError:
                    pass
                else:
                    phase = "planning" if match[2] == "planning" else "execution"
            tasks = tuple(
                ProjectRunTaskSummary(
                    id=task.id,
                    agent_id=task.agent_id,
                    status=task.status,
                    artifact_count=len(task.artifacts),
                )
                for task in run.tasks
            )
            items.append(
                ProjectRunSummary(
                    id=run.id,
                    plan_id=run.plan_id,
                    phase=phase,
                    cycle_id=cycle_id,
                    status=run.status,
                    created_at=job.created_at,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    task_count=len(tasks),
                    artifact_count=sum(task.artifact_count for task in tasks),
                    tasks=tasks,
                    run_url=f"/v1/agent-platform/runs/{run.id}",
                    plan_url=f"/v1/agent-platform/plans/{run.plan_id}",
                )
            )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            value = _Cursor(
                project_id=project_id,
                actor_id=actor.actor_id,
                workspace_id=actor.household_id,
                created_at=last.created_at,
                id=last.id,
            )
            next_cursor = base64.urlsafe_b64encode(value.model_dump_json().encode()).decode()
            next_cursor = next_cursor.rstrip("=")
        return ProjectRunPage(project_id=project_id, items=tuple(items), next_cursor=next_cursor)
