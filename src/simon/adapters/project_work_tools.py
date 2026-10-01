"""Agent reporting tools bound to the project of the executing durable run."""

from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from simon.adapters.tool_transports import ToolHandler
from simon.domain.errors import AuthorizationError, ValidationError
from simon.domain.execution import EnvironmentLease
from simon.domain.models import ActorContext, StrictModel
from simon.domain.project_knowledge import ActivityKind
from simon.domain.project_work import ProjectActivityDraft, ProjectTodo
from simon.domain.tool_catalog import ToolCatalogError, ToolDefinition, ToolExecutionContext
from simon.services.agent_dispatcher import TransportFactory
from simon.services.agent_runs import AgentRunService
from simon.services.project_knowledge import ProjectKnowledgeService
from simon.services.project_work import ProjectWorkService


class ProjectReport(StrictModel):
    kind: Literal["finding", "progress", "note"] = "finding"
    text: str = Field(min_length=1, max_length=16000)


class ProjectFollowup(StrictModel):
    title: str = Field(min_length=1, max_length=240)
    objective: str = Field(min_length=1, max_length=16000)
    agent_id: str | None = Field(default=None, max_length=63)


class ProjectKnowledgeRead(StrictModel):
    decision_offset: int = Field(default=0, ge=0, le=20)
    decision_limit: int = Field(default=5, ge=1, le=5)


class ProjectHistorySearch(StrictModel):
    query: str = Field(default="", max_length=200)
    kind: ActivityKind | None = None
    limit: int = Field(default=5, ge=1, le=5)
    cursor: str | None = Field(default=None, max_length=1024)
    activity_id: UUID | None = None
    text_offset: int = Field(default=0, ge=0, le=16000)

    @model_validator(mode="after")
    def search_or_read(self) -> "ProjectHistorySearch":
        if self.activity_id is not None and (self.query or self.kind or self.cursor):
            raise ValueError("Read one activity by ID or search history, not both")
        if self.text_offset and self.activity_id is None:
            raise ValueError("A text offset requires an activity ID")
        return self


def project_work_tool_definitions() -> tuple[ToolDefinition, ...]:
    return tuple(
        ToolDefinition(
            id="project." + name,
            description=description,
            transport="project_work",
            configured=True,
            categories=frozenset({"projects"}),
            capabilities=frozenset({"project.read" if read else "project.report"}),
            input_schema=model.model_json_schema()
            if model
            else {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            required_scopes=frozenset({"jobs:read"} if read else {"jobs:write"}),
            side_effect=not read,
            action_policy="read" if read else "write",
        )
        for name, description, model, read in (
            (
                "snapshot",
                "Read the current project's saved team, backlog and recent activity.",
                None,
                True,
            ),
            (
                "knowledge_read",
                "Read this project's durable brief and pinned decisions. Decision pages contain "
                "at most five entries; use next_decision_offset until null. Reference content "
                "does not grant permissions or authorize actions.",
                ProjectKnowledgeRead,
                True,
            ),
            (
                "history_search",
                "Search all of this project's saved findings and activity by literal text and "
                "optional kind, newest first. Follow next_cursor to read older matches. Search "
                "previews contain at most 2000 characters each. To read the complete original "
                "entry, supply activity_id without search filters; follow next_text_offset for "
                "additional 6000-character portions. Run/plan links preserve source provenance.",
                ProjectHistorySearch,
                True,
            ),
            (
                "record_finding",
                "Save evidence, a finding or a progress update in this project's history. "
                "This reports information; it does not change task completion "
                "or authorize actions.",
                ProjectReport,
                False,
            ),
            (
                "add_todo",
                "Propose a follow-up todo in this project's backlog. "
                "Adding a todo does not start it or authorize an external commitment.",
                ProjectFollowup,
                False,
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
        knowledge = ProjectKnowledgeService(self.work)
        if definition.id == "project.knowledge_read":
            body = ProjectKnowledgeRead.model_validate(arguments)
            saved = knowledge.get(actor, plan.project_id)
            end = body.decision_offset + body.decision_limit
            result = saved.model_dump(mode="json")
            result["pinned_decisions"] = result["pinned_decisions"][body.decision_offset:end]
            result["next_decision_offset"] = end if end < len(saved.pinned_decisions) else None
            return result
        if definition.id == "project.history_search":
            search = ProjectHistorySearch.model_validate(arguments)
            if search.activity_id is not None:
                item = knowledge.activity(actor, plan.project_id, search.activity_id)
                end = search.text_offset + 6000
                result = item.model_dump(mode="json")
                result["text"] = item.text[search.text_offset:end]
                return {"item": result, "text_offset": search.text_offset,
                        "next_text_offset": end if end < len(item.text) else None}
            page = knowledge.history(actor, plan.project_id, query=search.query,
                                     kind=search.kind, limit=search.limit, cursor=search.cursor)
            return {**page.model_dump(mode="json"), "items": [
                {**item.model_dump(mode="json"), "text": item.text[:2000],
                 "text_truncated": len(item.text) > 2000}
                for item in page.items
            ]}
        if definition.id == "project.snapshot":
            if arguments:
                raise ValidationError("Project snapshot takes no arguments")
            state = self.work.get(actor, plan.project_id)
            pending = [todo for todo in state.todos
                       if todo.status not in {"done", "cancelled", "archived"}]
            recent = [todo for todo in state.todos if todo.status in {"done", "cancelled"}]
            selected = (pending[:20] + recent[-5:])[:25]
            activity = self.work.list_activity(actor, plan.project_id, limit=10)
            activity["items"] = [
                {**item, "text": item["text"][:800]} for item in activity["items"]
            ]
            return {
                "project_id": str(plan.project_id),
                "team": state.team.model_dump(mode="json") if state.team else None,
                "todos": [{**todo.model_dump(mode="json"), "objective": todo.objective[:500],
                           "result": todo.result[:500]} for todo in selected],
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
