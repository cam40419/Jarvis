"""Durable project backlog, team, activity and bounded autonomy contracts."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from simon.domain.agent_profiles import AgentRoleDefinition
from simon.domain.models import StrictModel, utc_now


class ProjectTeam(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    agent_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    lead_agent_id: str = Field(min_length=1, max_length=63)
    max_parallel: int = Field(default=3, ge=1, le=128)
    roles: dict[str, str] = Field(default_factory=dict)
    members: dict[str, AgentRoleDefinition] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def valid_team(self) -> "ProjectTeam":
        if len(set(self.agent_ids)) != len(self.agent_ids):
            raise ValueError("Project team cannot contain duplicate profiles")
        if self.lead_agent_id not in self.agent_ids:
            raise ValueError("Project lead must belong to the project team")
        if not self.members.keys() <= set(self.agent_ids):
            raise ValueError("Project member configurations must belong to the team")
        if not self.roles.keys() <= set(self.agent_ids) or any(
            not role.strip() or len(role) > 2000 for role in self.roles.values()
        ):
            raise ValueError(
                "Project responsibilities must name team members and fit 2000 characters"
            )
        return self


class ProjectAutonomy(StrictModel):
    mode: Literal["manual", "scheduled"] = "manual"
    objective: str = Field(default="", max_length=16000)
    cadence_minutes: int = Field(default=60, ge=5, le=10080)
    max_cycles: int = Field(default=10, ge=1, le=100)
    model_budget_usd: float | None = Field(default=None, gt=0, le=10000, allow_inf_nan=False)
    paused: bool = False

    @model_validator(mode="after")
    def bounded_schedule(self) -> "ProjectAutonomy":
        if self.mode == "scheduled" and (
            not self.objective.strip() or self.model_budget_usd is None
        ):
            raise ValueError("Scheduled work requires a standing objective and finite model budget")
        return self


class ProjectTodo(StrictModel):
    id: str = Field(
        default_factory=lambda: "todo-" + uuid4().hex, pattern=r"^[a-z][a-z0-9_.-]{0,79}$"
    )
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=16000)
    agent_id: str | None = Field(default=None, max_length=63)
    depends_on: tuple[str, ...] = Field(default=(), max_length=100)
    status: Literal[
        "todo", "ready", "running", "done", "blocked", "cancelled", "unknown", "archived"
    ] = "todo"
    progress: int = Field(default=0, ge=0, le=100)
    result: str = Field(default="", max_length=16000)
    error: str | None = Field(default=None, max_length=2000)
    cycle_id: UUID | None = None
    plan_id: UUID | None = None
    run_id: UUID | None = None
    run_task_id: str | None = Field(default=None, max_length=80)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)


class ProjectActivityDraft(StrictModel):
    kind: Literal[
        "finding", "progress", "note", "decision", "blocked", "cycle", "task", "configuration"
    ] = "note"
    text: str = Field(min_length=1, max_length=16000)
    agent_id: str | None = Field(default=None, max_length=63)
    task_id: str | None = Field(default=None, max_length=80)
    run_id: UUID | None = None
    plan_id: UUID | None = None
    action_id: UUID | None = None


class ProjectActivity(ProjectActivityDraft):
    id: UUID
    project_id: UUID
    sequence: int = Field(ge=1)
    created_at: AwareDatetime


class ProjectCycle(StrictModel):
    id: UUID = Field(default_factory=uuid4)
    number: int = Field(ge=1)
    revision: int = Field(default=1, ge=1)
    instruction: str = Field(min_length=1, max_length=16000)
    phase: Literal[
        "starting", "planning", "ready", "executing", "completed", "blocked", "unknown", "cancelled"
    ] = "starting"
    automatic: bool = False
    execution_approved: bool = False
    model_budget_usd: float | None = Field(default=None, gt=0, le=10000, allow_inf_nan=False)
    model_reserved_usd: float = Field(default=0, ge=0, allow_inf_nan=False)
    planning_plan_id: UUID | None = None
    planning_run_id: UUID | None = None
    execution_plan_id: UUID | None = None
    execution_run_id: UUID | None = None
    error: str | None = Field(default=None, max_length=2000)
    started_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    finished_at: AwareDatetime | None = None


class ProjectCycleUpdate(StrictModel):
    cycle: ProjectCycle
    # Upsert these rows; never replace unrelated user-created backlog entries.
    todos: tuple[ProjectTodo, ...] = Field(default=(), max_length=100)
    activity: tuple[ProjectActivityDraft, ...] = Field(default=(), max_length=50)


class ProjectWorkState(StrictModel):
    project_id: UUID
    workspace_id: UUID
    actor_id: UUID
    version: int = Field(default=0, ge=0)
    team: ProjectTeam | None = None
    autonomy: ProjectAutonomy = Field(default_factory=ProjectAutonomy)
    todos: tuple[ProjectTodo, ...] = Field(default=(), max_length=500)
    active_cycle: ProjectCycle | None = None
    last_cycle: ProjectCycle | None = None
    cycle_count: int = Field(default=0, ge=0)
    scheduled_cycles_used: int = Field(default=0, ge=0)
    activity_count: int = Field(default=0, ge=0)
    blocked_reasons: tuple[str, ...] = ()
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    next_cycle_at: AwareDatetime | None = None


class ConfigureProjectWork(StrictModel):
    expected_version: int = Field(ge=0)
    team: ProjectTeam | None = None
    autonomy: ProjectAutonomy | None = None


class ProjectWorkControl(StrictModel):
    expected_version: int = Field(ge=0)
    action: Literal["pause", "resume", "run_ready", "discard", "acknowledge"]
    note: str = Field(default="", max_length=2000)
