from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from simon.domain.models import ActorContext
from simon.domain.workflows import (
    ControlWorkflow,
    ControlWorkflowSchedule,
    ControlWorkflowTrigger,
    SaveWorkflow,
    SaveWorkflowSchedule,
    SaveWorkflowTrigger,
    StartWorkflow,
    WorkflowDefinition,
    WorkflowEvent,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowTrigger,
)
from simon.services.workflows import WorkflowService


def workflow_router(
    service: WorkflowService, authenticate: Callable[[Request], ActorContext]
) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["workflows"])

    @router.get("/workflows/health")
    def health(actor: Annotated[ActorContext, Depends(authenticate)]) -> dict[str, Any]:
        return service.health(actor)

    @router.get("/workflows")
    def definitions(
        actor: Annotated[ActorContext, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> tuple[WorkflowDefinition, ...]:
        return service.definitions(actor, offset, limit)

    @router.post("/workflows", status_code=201)
    def create(
        body: SaveWorkflow, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> WorkflowDefinition:
        return service.save(actor, body)

    @router.get("/workflows/{identifier}")
    def definition(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> WorkflowDefinition:
        return service.definition(actor, identifier, version)

    @router.post("/workflows/{identifier}/versions", status_code=201)
    def update(
        identifier: UUID, body: SaveWorkflow, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> WorkflowDefinition:
        return service.save(actor, body, identifier)

    @router.delete("/workflows/{identifier}")
    def delete(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        expected_version: Annotated[int, Query(ge=1)],
    ) -> dict[str, Any]:
        return service.delete(actor, identifier, expected_version)

    @router.post("/workflows/{identifier}/runs", status_code=201)
    def start(
        identifier: UUID, body: StartWorkflow, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> WorkflowRun:
        return service.start(actor, identifier, body)

    @router.post("/workflows/{identifier}/schedules", status_code=201)
    def create_schedule(
        identifier: UUID,
        body: SaveWorkflowSchedule,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> WorkflowSchedule:
        return service.create_schedule(actor, identifier, body)

    @router.get("/workflow-schedules")
    def schedules(
        actor: Annotated[ActorContext, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> tuple[WorkflowSchedule, ...]:
        return service.schedules(actor, offset, limit)

    @router.post("/workflow-schedules/{identifier}/control")
    def control_schedule(
        identifier: UUID,
        body: ControlWorkflowSchedule,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> WorkflowSchedule:
        return service.control_schedule(actor, identifier, body)

    @router.delete("/workflow-schedules/{identifier}")
    def delete_schedule(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        expected_version: Annotated[int, Query(ge=1)],
    ) -> dict[str, Any]:
        return service.delete_schedule(actor, identifier, expected_version)

    @router.get("/workflow-triggers")
    def triggers(
        actor: Annotated[ActorContext, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> tuple[WorkflowTrigger, ...]:
        return service.triggers(actor, offset, limit)

    @router.post("/workflows/{identifier}/triggers", status_code=201)
    def create_trigger(
        identifier: UUID,
        body: SaveWorkflowTrigger,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> WorkflowTrigger:
        return service.create_trigger(actor, identifier, body)

    @router.post("/workflow-triggers/{identifier}/control")
    def control_trigger(
        identifier: UUID,
        body: ControlWorkflowTrigger,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> WorkflowTrigger:
        return service.control_trigger(actor, identifier, body)

    @router.delete("/workflow-triggers/{identifier}")
    def delete_trigger(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        expected_version: Annotated[int, Query(ge=1)],
    ) -> dict[str, Any]:
        return service.delete_trigger(actor, identifier, expected_version)

    @router.get("/workflow-runs")
    def runs(
        actor: Annotated[ActorContext, Depends(authenticate)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> tuple[WorkflowRun, ...]:
        return service.runs(actor, offset, limit)

    @router.get("/workflow-runs/{identifier}")
    def run(identifier: UUID, actor: Annotated[ActorContext, Depends(authenticate)]) -> WorkflowRun:
        return service.get(actor, identifier)

    @router.get("/workflow-runs/{identifier}/events")
    def events(
        identifier: UUID,
        actor: Annotated[ActorContext, Depends(authenticate)],
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> tuple[WorkflowEvent, ...]:
        return service.events(actor, identifier, after)

    @router.post("/workflow-runs/{identifier}/control")
    def control(
        identifier: UUID,
        body: ControlWorkflow,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> WorkflowRun:
        return service.control(actor, identifier, body)

    return router
