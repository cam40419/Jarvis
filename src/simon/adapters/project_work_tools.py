"""Agent reporting tools bound to the project of the executing durable run."""

from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from simon.adapters.tool_transports import ToolHandler
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.execution import EnvironmentLease
from simon.domain.models import ActorContext, StrictModel
from simon.domain.project_work import ProjectActivityDraft, ProjectTodo
from simon.domain.tool_catalog import ToolCatalogError, ToolDefinition, ToolExecutionContext
from simon.services.agent_dispatcher import TransportFactory
from simon.services.agent_runs import AgentRunService
from simon.services.project_work import ProjectWorkService


class ProjectReport(StrictModel):
    kind: Literal["finding", "progress", "note"] = "finding"
    text: str = Field(min_length=1, max_length=16000)


class ProjectFollowup(StrictModel):
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=16000)
    agent_id: str | None = Field(default=None, max_length=63)


def project_work_tool_definitions() -> tuple[ToolDefinition, ...]:
    return tuple(
        ToolDefinition(
            id="project." + name,
            description=description,
            transport="project_work",
            configured=True,
            categories=frozenset({"projects"}),
            capabilities=frozenset({"project.read" if name == "snapshot" else "project.report"}),
            input_schema=model.model_json_schema()
            if model
            else {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            required_scopes=frozenset({"jobs:read"} if name == "snapshot" else {"jobs:write"}),
            side_effect=name != "snapshot",
            action_policy="read" if name == "snapshot" else "write",
        )
        for name, description, model in (
            (
                "snapshot",
                "Read the current project's saved team, backlog and recent activity.",
                None,
            ),
            (
                "record_finding",
                "Save evidence, a finding or a progress update in this project's history. "
                "This reports information; it does not change task completion "
                "or authorize actions.",
                ProjectReport,
            ),
            (
                "add_todo",
                "Propose a follow-up todo in this project's backlog. "
                "Adding a todo does not start it or authorize an external commitment.",
                ProjectFollowup,
            ),
        )
    )


def project_tool_configuration_reason(tool: ToolDefinition) -> str | None:
    canonical = next((item for item in project_work_tool_definitions() if item.id == tool.id), None)
    if canonical is None or tool.transport != "project_work":
        return "Unknown project reporting tool"
    fields = ("input_schema", "side_effect", "action_policy", "required_scopes")
    if any(getattr(tool, field) != getattr(canonical, field) for field in fields):
        return "Project reporting tool contract does not match its installed definition"
    return None


class ProjectWorkToolTransport:
    def __init__(
        self,
        work: ProjectWorkService,
        runs: AgentRunService,
        *,
        actor: ActorContext,
        run_id: UUID,
        revalidate: Callable[[], ActorContext],
    ) -> None:
        self.work, self.runs, self.actor = work, runs, actor
        self.run_id, self.revalidate = run_id, revalidate

    def __call__(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        if (context.actor_id, context.household_id, context.run_id) != (
            self.actor.actor_id,
            self.actor.household_id,
            self.run_id,
        ):
            raise AuthorizationError("Project tool owner changed")
        if reason := project_tool_configuration_reason(definition):
            raise ToolCatalogError(reason)
        if (
            not definition.enabled
            or not definition.configured
            or definition.id not in context.allowed_tool_ids
        ):
            raise AuthorizationError("Project tool is not granted to this assignment")
        if definition.side_effect and context.authorized_action != "write":
            raise AuthorizationError("Project reporting requires a write grant")
        actor = self.revalidate()
        if (actor.actor_id, actor.household_id) != (self.actor.actor_id, self.actor.household_id):
            raise AuthorizationError("Project account changed")
        actor = actor.model_copy(
            update={"scopes": actor.scopes & context.scopes & self.actor.scopes}
        )
        if not definition.required_scopes <= actor.scopes:
            raise AuthorizationError("Project reporting permission changed")
        run = self.runs.get(actor, self.run_id)
        plan = self.runs.platform.get(actor, run.plan_id)
        if plan.project_id is None:
            raise ValidationError("This tool requires a project-assigned run")
        if context.agent_id not in {task.agent_id for task in run.tasks}:
            raise AuthorizationError("Agent is not assigned to this project run")
        key = f"agent:{run.id}:{context.invocation_id}"
        if definition.id == "project.snapshot":
            if arguments:
                raise ValidationError("Project snapshot takes no arguments")
            state = self.work.get(actor, plan.project_id)
            pending = [
                todo for todo in state.todos if todo.status not in {"done", "cancelled", "archived"}
            ]
            recent = [todo for todo in state.todos if todo.status in {"done", "cancelled"}]
            selected = (pending[:20] + recent[-5:])[:25]
            activity = self.work.list_activity(actor, plan.project_id, limit=10)
            activity["items"] = [{**item, "text": item["text"][:800]} for item in activity["items"]]
            return {
                "project_id": str(plan.project_id),
                "team": state.team.model_dump(mode="json") if state.team else None,
                "todos": [
                    {
                        **todo.model_dump(mode="json"),
                        "objective": todo.objective[:500],
                        "result": todo.result[:500],
                    }
                    for todo in selected
                ],
                "omitted_todos": len(state.todos) - len(selected),
                "activity": activity,
            }
        if definition.id == "project.record_finding":
            report = ProjectReport.model_validate(arguments)
            self.work.record_activity(
                actor,
                plan.project_id,
                ProjectActivityDraft(
                    kind=report.kind,
                    text=report.text,
                    agent_id=context.agent_id,
                    run_id=run.id,
                    plan_id=plan.id,
                ),
                idempotency_key=key,
            )
            return {"saved": True, "project_id": str(plan.project_id)}
        followup = ProjectFollowup.model_validate(arguments)
        todo = ProjectTodo(
            id="todo-" + context.invocation_id.hex,
            title=followup.title,
            objective=followup.objective,
            agent_id=followup.agent_id,
            created_at=run.started_at or self.runs.job(run.id).created_at,
            updated_at=run.started_at or self.runs.job(run.id).created_at,
        )
        self.work.add_todo(actor, plan.project_id, todo, idempotency_key=key)
        return {"saved": True, "todo_id": todo.id, "status": "todo"}


def project_transport_factory(
    base: TransportFactory,
    work: ProjectWorkService,
    runs: AgentRunService,
) -> TransportFactory:
    def factory(
        actor: ActorContext,
        run_id: UUID,
        revalidate: Callable[[], ActorContext],
        lease: EnvironmentLease | None,
    ) -> dict[str, ToolHandler]:
        return {
            **base(actor, run_id, revalidate, lease),
            "project_work": ProjectWorkToolTransport(
                work,
                runs,
                actor=actor,
                run_id=run_id,
                revalidate=revalidate,
            ),
        }

    return factory
