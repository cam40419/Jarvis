"""Separate human workflow controls from narrowly scoped outbound runner calls."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from simon.domain.errors import AuthorizationError
from simon.domain.models import ActorContext
from simon.domain.native_execution import (
    CreateExecutionSchedule,
    EnrollExecutionRunner,
    ExecutionCommand,
    NativeExecutionPolicy,
    NativeExecutionRunner,
    NativeExecutionSchedule,
    NativeTaskWorkflow,
    RunnerLeaseCommand,
    SignalExecution,
    StartExecution,
    UpdateExecutionPolicy,
    UpdateExecutionSchedule,
    UpdateTaskWorkflow,
)
from simon.domain.native_projects import NativeCommand
from simon.services.native_execution import NativeExecutionService


class CreateScheduleRequest(CreateExecutionSchedule):
    task_id: UUID


def native_execution_router(
    service: NativeExecutionService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v2/projects/{project_id}/execution", tags=["native execution"])

    def actor(
        request: Request, x_workspace_id: Annotated[UUID | None, Header()] = None
    ) -> ActorContext:
        current = authenticate(request)
        if x_workspace_id is not None and current.workspace_id != x_workspace_id:
            raise AuthorizationError("Workspace changed. Reload before continuing.")
        return current

    @router.get("")
    def view(project_id: UUID, current: Annotated[ActorContext, Depends(actor)]) -> dict[str, Any]:
        return service.view(current, project_id)

    @router.put("/policy")
    def policy(
        project_id: UUID,
        body: UpdateExecutionPolicy,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> NativeExecutionPolicy:
        return service.update_policy(current, project_id, body)

    @router.get("/tasks/{task_id}/workflow")
    def workflow(
        project_id: UUID, task_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.task_workflow(current, project_id, task_id)

    @router.put("/tasks/{task_id}/workflow")
    def update_workflow(
        project_id: UUID,
        task_id: UUID,
        body: UpdateTaskWorkflow,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> NativeTaskWorkflow:
        return service.update_workflow(current, project_id, task_id, body)

    @router.post("/tasks/{task_id}/runs", status_code=202)
    def start(
        project_id: UUID,
        task_id: UUID,
        body: StartExecution,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.start(current, project_id, task_id, body)

    @router.get("/runs")
    def runs(
        project_id: UUID,
        current: Annotated[ActorContext, Depends(actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        return service.list_runs(current, project_id, offset, limit)

    @router.get("/runs/{run_id}")
    def detail(
        project_id: UUID, run_id: UUID, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.detail(current, project_id, run_id)

    @router.get("/runs/{run_id}/events")
    def events(
        project_id: UUID,
        run_id: UUID,
        current: Annotated[ActorContext, Depends(actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        return service.events(current, project_id, run_id, offset, limit)

    @router.post("/runs/{run_id}/cancel")
    def cancel(
        project_id: UUID,
        run_id: UUID,
        body: ExecutionCommand,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.cancel(current, project_id, run_id, body)

    @router.post("/runs/{run_id}/retry", status_code=202)
    def retry(
        project_id: UUID,
        run_id: UUID,
        body: ExecutionCommand,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.retry(current, project_id, run_id, body)

    @router.post("/runs/{run_id}/signals")
    def signal(
        project_id: UUID,
        run_id: UUID,
        body: SignalExecution,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.signal(current, project_id, run_id, body)

    @router.get("/schedules")
    def schedules(
        project_id: UUID,
        current: Annotated[ActorContext, Depends(actor)],
        offset: int = Query(0, ge=0, le=1_000_000),
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        return service.list_schedules(current, project_id, offset, limit)

    @router.post("/schedules", status_code=201)
    def create_schedule(
        project_id: UUID,
        body: CreateScheduleRequest,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> NativeExecutionSchedule:
        command = CreateExecutionSchedule.model_validate(body.model_dump(exclude={"task_id"}))
        return service.create_schedule(current, project_id, body.task_id, command)

    @router.put("/schedules/{schedule_id}")
    def update_schedule(
        project_id: UUID,
        schedule_id: UUID,
        body: UpdateExecutionSchedule,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> NativeExecutionSchedule:
        return service.update_schedule(current, project_id, schedule_id, body)

    @router.post("/runners", status_code=201)
    def enroll_runner(
        project_id: UUID,
        body: EnrollExecutionRunner,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.enroll_runner(current, project_id, body)

    @router.post("/runners/{runner_id}/revoke")
    def revoke_runner(
        project_id: UUID,
        runner_id: UUID,
        body: ExecutionCommand,
        current: Annotated[ActorContext, Depends(actor)],
    ) -> dict[str, Any]:
        return service.revoke_runner(current, project_id, runner_id, body)

    @router.get("/operations/{key}")
    def receipt(
        project_id: UUID, key: str, current: Annotated[ActorContext, Depends(actor)]
    ) -> dict[str, Any]:
        return service.operation_receipt(current, project_id, key)

    return router


def native_execution_worker_router(service: NativeExecutionService) -> APIRouter:
    router = APIRouter(prefix="/v2/execution-worker", tags=["execution runner"])

    def runner(request: Request) -> NativeExecutionRunner:
        authorization = request.headers.getlist("authorization")
        if (
            "cookie" in request.headers
            or "x-csrf-token" in request.headers
            or len(authorization) != 1
            or not authorization[0].startswith("Bearer ")
        ):
            raise AuthorizationError("Use only an execution runner bearer credential.")
        token = authorization[0][7:]
        if not token or any(character.isspace() for character in token):
            raise AuthorizationError("Use only an execution runner bearer credential.")
        return service.runner(token)

    @router.post("/claim")
    def claim(
        body: NativeCommand, current: Annotated[NativeExecutionRunner, Depends(runner)]
    ) -> dict[str, Any] | None:
        return service.claim(current, body)

    @router.post("/heartbeat")
    def heartbeat(
        body: RunnerLeaseCommand, current: Annotated[NativeExecutionRunner, Depends(runner)]
    ) -> dict[str, Any]:
        return service.heartbeat(current, body)

    @router.post("/step")
    def step(
        body: RunnerLeaseCommand, current: Annotated[NativeExecutionRunner, Depends(runner)]
    ) -> dict[str, Any]:
        return service.step(current, body)

    @router.get("/runs/{run_id}")
    def status(
        run_id: UUID, current: Annotated[NativeExecutionRunner, Depends(runner)]
    ) -> dict[str, Any]:
        return service.worker_status(current, run_id)

    return router
