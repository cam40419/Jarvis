"""Authenticated configuration discovery and durable team planning endpoints."""

from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field

from simon.domain.agent_platform import AgentProfile, AgentTaskSpec, AgentTeamPlan, PlanTeamRequest
from simon.domain.agent_profiles import AgentProfileRecord, CreateAgentProfile, UpdateAgentProfile
from simon.domain.agent_runs import AgentRun, ReconcileAgentRun, StartAgentRun
from simon.domain.artifact_reviews import (
    AcceptArtifactReview,
    ArtifactAcceptance,
    ArtifactReview,
    RecordArtifactReview,
)
from simon.domain.artifacts import ArtifactPreview
from simon.domain.errors import NotFoundError
from simon.domain.models import ActorContext, StrictModel
from simon.services.agent_calendar import AgentCalendarReceipt, AgentCalendarService
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_prompts import PreparedAgentPrompt, render_agent_prompt
from simon.services.agent_runs import AgentRunService
from simon.services.artifact_reviews import ArtifactReviewService
from simon.services.artifacts import ArtifactStore


class PromptPreviewRequest(StrictModel):
    task: AgentTaskSpec
    dependency_outputs: dict[str, str] = Field(default_factory=dict)
    context_id: str | None = None


def agent_platform_router(
    service: AgentPlatformService,
    authenticate: Callable[[Request], ActorContext],
    runs: AgentRunService | None = None,
    calendar: AgentCalendarService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/agent-platform", tags=["agent platform"])

    @router.get("/catalog")
    def catalog(actor: Annotated[ActorContext, Depends(authenticate)]) -> dict[str, Any]:
        result = service.catalog(actor)
        result["execution_enabled"] = runs.enabled if runs else False
        return result

    @router.post("/environments/{identifier}/test")
    def test_environment(
        identifier: str,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> dict[str, Any]:
        service.authorize(actor, write=True)
        return service.test_environment(actor, identifier)

    @router.post("/agents", status_code=201)
    def create_agent(
        body: CreateAgentProfile,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> AgentProfileRecord:
        return service.agent_profiles.create(actor, body)

    @router.patch("/agents/{identifier}")
    def update_agent(
        identifier: str,
        body: UpdateAgentProfile,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> AgentProfileRecord:
        return service.agent_profiles.update(actor, identifier, body)

    @router.get("/agents/{identifier}")
    def agent(
        identifier: str,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> AgentProfile:
        configured = next(
            (
                profile
                for profile in service.catalog(actor)["agents"]
                if profile["id"] == identifier
            ),
            None,
        )
        if configured is None:
            raise NotFoundError("Agent profile not found")
        return AgentProfile.model_validate(configured)

    @router.post("/agents/{identifier}/prompt-preview")
    def prompt_preview(
        identifier: str,
        body: PromptPreviewRequest,
        actor: Annotated[ActorContext, Depends(authenticate)],
    ) -> PreparedAgentPrompt:
        profile = agent(identifier, actor)
        service._context(actor, body.context_id)
        context_name = next(
            (
                context.name
                for context in service.manifest.contexts
                if context.id == body.context_id
            ),
            "",
        )
        return render_agent_prompt(profile, body.task, body.dependency_outputs, context_name)

    @router.post("/plans", status_code=201)
    def plan(
        body: PlanTeamRequest, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> AgentTeamPlan:
        return service.plan(actor, body)

    @router.get("/plans")
    def plans(actor: Annotated[ActorContext, Depends(authenticate)]) -> tuple[AgentTeamPlan, ...]:
        return service.list(actor)

    @router.get("/plans/{identifier}")
    def get(
        identifier: UUID, actor: Annotated[ActorContext, Depends(authenticate)]
    ) -> AgentTeamPlan:
        return service.get(actor, identifier)

    if runs is not None:
        reviews = ArtifactReviewService(runs)

        if calendar is not None:

            @router.get("/runs/{identifier}/calendar-actions")
            def calendar_actions(
                identifier: UUID,
                actor: Annotated[ActorContext, Depends(authenticate)],
            ) -> tuple[AgentCalendarReceipt, ...]:
                runs.get(actor, identifier)
                return calendar.list_for_run(actor, identifier)

        @router.get("/runs/{identifier}/reviews")
        def list_reviews(
            identifier: UUID,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> tuple[ArtifactReview, ...]:
            return reviews.list(actor, identifier)

        @router.post("/runs/{identifier}/reviews", status_code=201)
        def record_review(
            identifier: UUID,
            body: RecordArtifactReview,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> ArtifactReview:
            return reviews.record(actor, identifier, body)

        @router.get("/reviews/{identifier}/acceptance")
        def get_acceptance(
            identifier: UUID,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> ArtifactAcceptance:
            return reviews.acceptance(actor, identifier)

        @router.post("/reviews/{identifier}/accept")
        def accept_review(
            identifier: UUID,
            body: AcceptArtifactReview,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> ArtifactAcceptance:
            return reviews.accept(actor, identifier, body)

        @router.post("/plans/{identifier}/runs", status_code=201)
        def start_run(
            identifier: UUID,
            body: StartAgentRun,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> AgentRun:
            return runs.start(actor, identifier, body)

        @router.get("/runs")
        def list_runs(
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> tuple[AgentRun, ...]:
            return runs.list(actor)

        @router.get("/runs/{identifier}")
        def get_run(
            identifier: UUID,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> AgentRun:
            return runs.get(actor, identifier)

        @router.post("/runs/{identifier}/cancel")
        def cancel_run(
            identifier: UUID,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> AgentRun:
            return runs.cancel(actor, identifier)

        @router.post("/runs/{identifier}/reconcile")
        def reconcile_run(
            identifier: UUID,
            body: ReconcileAgentRun,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> AgentRun:
            return runs.reconcile(actor, identifier, body)

        @router.get("/runs/{identifier}/artifacts/{artifact_id}")
        def artifact(
            identifier: UUID,
            artifact_id: UUID,
            actor: Annotated[ActorContext, Depends(authenticate)],
        ) -> Response:
            run = runs.get(actor, identifier)
            reference = next(
                (item for task in run.tasks for item in task.artifacts if item.id == artifact_id),
                None,
            )
            if reference is None:
                raise NotFoundError("Agent artifact not found")
            content = ArtifactStore(service.state_dir / "artifacts").read(reference)
            return Response(
                content,
                media_type=reference.media_type,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{reference.name}"'
                        if reference.name.isascii()
                        else "attachment; filename*=UTF-8''" + quote(reference.name, safe="")
                    )
                },
            )

        @router.get("/runs/{identifier}/artifacts/{artifact_id}/preview")
        def artifact_preview(
            identifier: UUID,
            artifact_id: UUID,
            actor: Annotated[ActorContext, Depends(authenticate)],
            response: Response,
        ) -> ArtifactPreview:
            run = runs.get(actor, identifier)
            reference = next(
                (item for task in run.tasks for item in task.artifacts if item.id == artifact_id),
                None,
            )
            if reference is None:
                raise NotFoundError("Agent artifact not found")
            response.headers["Cache-Control"] = "no-store"
            return ArtifactStore(service.state_dir / "artifacts").preview(reference)

    return router
