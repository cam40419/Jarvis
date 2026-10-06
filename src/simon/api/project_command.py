"""Authenticated project teams, lead requests, persistent backlog and activity."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from simon.api.project_boards import board_blockers
from simon.api.project_knowledge import project_knowledge_router
from simon.domain.errors import DomainError, ValidationError
from simon.domain.models import ActorContext, StrictModel
from simon.domain.project_continuity import (
    CancelProjectRequest,
    ContinueProjectWork,
    CreateProjectSchedule,
    CreateProjectWait,
    QueueProjectRequest,
    ReplyProjectWait,
    SetProjectSchedule,
)
from simon.domain.project_work import (
    ConfigureProjectWork,
    ProjectActivityDraft,
    ProjectTodo,
    ProjectWorkControl,
    ProjectWorkState,
)
from simon.services.project_coordinator import ProjectCoordinator
from simon.services.project_history import ProjectHistoryService, ProjectRunPage
from simon.services.project_presentation import project_presentation


class ProjectCommand(StrictModel):
    instruction: str = Field(min_length=1, max_length=16000, pattern=r"\S")
    idempotency_key: str = Field(min_length=8, max_length=180)
    replace_failed: bool = False
    expected_version: int | None = Field(default=None, ge=0)


class AddProjectTodo(StrictModel):
    todo: ProjectTodo
    idempotency_key: str = Field(min_length=8, max_length=180)


class EditProjectTodo(StrictModel):
    todo: ProjectTodo
    expected_version: int = Field(ge=1)


class AddProjectActivity(StrictModel):
    entry: ProjectActivityDraft
    idempotency_key: str = Field(min_length=8, max_length=180)


def project_command_router(
    coordinator: ProjectCoordinator,
    authenticate: Callable[[Request], ActorContext],
) -> APIRouter:
    router = APIRouter(prefix="/v1/projects/{project_id}", tags=["project work"])
    work, runs = coordinator.work, coordinator.runs
    router.include_router(project_knowledge_router(work, authenticate))
    history = ProjectHistoryService(work, runs)

    @router.get("/runs")
    def run_history(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        limit: int = Query(20, ge=1, le=50),
        cursor: str | None = Query(None, max_length=512),
    ) -> ProjectRunPage:
        return history.list(actor, project_id, limit=limit, cursor=cursor)

    @router.get("/command")
    def snapshot(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        state = work.get(actor, project_id)
        project = work.project_resolver(actor, project_id)
        cycles = [cycle for cycle in (state.active_cycle, state.last_cycle) if cycle]
        plan_ids = {
            identifier
            for cycle in cycles
            for identifier in (cycle.planning_plan_id, cycle.execution_plan_id)
            if identifier
        }
        run_ids = {
            identifier
            for cycle in cycles
            for identifier in (cycle.planning_run_id, cycle.execution_run_id)
            if identifier
        }
        plans, executions = [], []
        run_records = []
        blockers = list(state.blocked_reasons)
        if coordinator.boards is not None:
            blockers.extend(
                board_blockers(
                    coordinator.boards,
                    actor,
                    coordinator.boards.get(actor, project_id),
                )
            )
        if not runs.enabled:
            blockers.append("Agent execution is disabled on this server.")
        if state.team is None:
            blockers.append("Choose a project team and lead to start.")
        else:
            try:
                coordinator.validate_team(actor, state.team)
            except DomainError as error:
                blockers.append(str(error))
        for identifier in sorted(plan_ids, key=str):
            try:
                plans.append(coordinator.platform.get(actor, identifier).model_dump(mode="json"))
            except DomainError as error:
                blockers.append(str(error))
        for identifier in sorted(run_ids, key=str):
            try:
                execution = runs.get(actor, identifier)
                run_records.append(execution)
                executions.append(execution.model_dump(mode="json"))
            except DomainError as error:
                blockers.append(str(error))
        return {
            "project": {
                "id": str(project.id),
                "name": project.subject,
                "description": project.content,
            },
            "state": state.model_dump(mode="json"),
            "member_profiles": coordinator.member_profiles(actor, project_id),
            "activity": work.list_activity(actor, project_id),
            "plans": plans,
            "runs": executions,
            "external_actions": [
                action.model_dump(mode="json")
                for identifier in run_ids
                for action in coordinator.external_actions.list_for_run(actor, identifier)
            ]
            if coordinator.external_actions
            else [],
            "blocked_reasons": list(dict.fromkeys(blockers)),
            "continuity": continuity_snapshot(project_id, actor),
            "presentation": project_presentation(
                state,
                run_records,
                blockers=blockers,
                recovery=coordinator.retry_readiness(actor, project_id),
            ),
        }

    @router.post("/command", status_code=202)
    def command(
        project_id: UUID,
        body: ProjectCommand,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectWorkState:
        if not runs.enabled:
            raise ValidationError("Enable agent execution before asking a project lead to work")
        return work.request_cycle(
            actor,
            project_id,
            body.instruction,
            body.idempotency_key,
            # A user request always follows manual plan review. Only the scheduler
            # can consume scheduled-cycle allowances and approve automatic work.
            automatic=False,
            replace_failed=body.replace_failed,
            expected_version=body.expected_version,
        )

    @router.get("/continuity")
    def continuity_snapshot(
        project_id: UUID, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> dict[str, Any]:
        return {
            **coordinator.continuity.snapshot(actor, project_id),
            "continuation": coordinator.continuation_readiness(actor, project_id),
        }

    @router.post("/requests", status_code=202)
    def queue_request(
        project_id: UUID,
        body: QueueProjectRequest,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        if not runs.enabled:
            raise ValidationError("Enable agent execution before queuing project work")
        return coordinator.continuity.queue(actor, project_id, body)

    @router.post("/requests/{request_id}/cancel")
    def cancel_request(
        project_id: UUID,
        request_id: UUID,
        body: CancelProjectRequest,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return coordinator.continuity.cancel_request(
            actor, project_id, request_id, body.expected_version
        )

    @router.post("/waits", status_code=201)
    def create_wait(
        project_id: UUID,
        body: CreateProjectWait,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return coordinator.continuity.create_wait(actor, project_id, body)

    @router.post("/requests/{request_id}/retry", status_code=202)
    def retry_request(
        project_id: UUID,
        request_id: UUID,
        body: CancelProjectRequest,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return coordinator.continuity.retry_request(
            actor, project_id, request_id, body.expected_version
        )

    @router.post("/waits/{wait_id}/reply", status_code=202)
    def reply_wait(
        project_id: UUID,
        wait_id: UUID,
        body: ReplyProjectWait,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return coordinator.continuity.reply(actor, project_id, wait_id, body)

    @router.post("/waits/{wait_id}/cancel")
    def cancel_wait(
        project_id: UUID,
        wait_id: UUID,
        body: CancelProjectRequest,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return coordinator.continuity.cancel_wait(actor, project_id, wait_id, body.expected_version)

    @router.post("/schedules", status_code=201)
    def create_schedule(
        project_id: UUID,
        body: CreateProjectSchedule,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        if not runs.enabled:
            raise ValidationError("Enable agent execution before scheduling project work")
        return coordinator.continuity.schedule(actor, project_id, body)

    @router.patch("/schedules/{schedule_id}")
    def set_schedule(
        project_id: UUID,
        schedule_id: UUID,
        body: SetProjectSchedule,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        return coordinator.continuity.set_schedule(actor, project_id, schedule_id, body)

    @router.post("/continue", status_code=202)
    def continue_work(
        project_id: UUID,
        body: ContinueProjectWork,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectWorkState:
        if not runs.enabled:
            raise ValidationError("Enable agent execution before continuing project work")
        return coordinator.continue_saved(
            actor,
            project_id,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
        )

    @router.patch("/team")
    def configure(
        project_id: UUID,
        body: ConfigureProjectWork,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectWorkState:
        return work.configure(actor, project_id, body)

    @router.post("/control")
    def control(
        project_id: UUID,
        body: ProjectWorkControl,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectWorkState:
        return work.control(actor, project_id, body)

    @router.post("/todos", status_code=201)
    def add_todo(
        project_id: UUID,
        body: AddProjectTodo,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectWorkState:
        return work.add_todo(actor, project_id, body.todo, idempotency_key=body.idempotency_key)

    @router.get("/todos/archived")
    def archived(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0, le=10000000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        return work.list_archived(actor, project_id, offset=offset, limit=limit)

    @router.patch("/todos/{todo_id}")
    def edit_todo(
        project_id: UUID,
        todo_id: str,
        body: EditProjectTodo,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectWorkState:
        if todo_id != body.todo.id:
            raise ValidationError("Project task identity does not match")
        return work.update_todo(
            actor, project_id, body.todo, expected_version=body.expected_version
        )

    @router.get("/activity")
    def activity(
        project_id: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0, le=10000000)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, Any]:
        return work.list_activity(actor, project_id, offset=offset, limit=limit)

    @router.post("/activity", status_code=201)
    def note(
        project_id: UUID,
        body: AddProjectActivity,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> ProjectWorkState:
        return work.record_activity(
            actor, project_id, body.entry, idempotency_key=body.idempotency_key
        )

    return router
