"""Persist review evidence and atomically advance an owner's accepted deliverable."""

from uuid import UUID, uuid5

from simon.domain.artifact_reviews import (
    AcceptArtifactReview,
    AcceptedRevision,
    ArtifactAcceptance,
    ArtifactReview,
    RecordArtifactReview,
)
from simon.domain.errors import IdempotencyConflictError, InvalidTransitionError, NotFoundError
from simon.domain.models import ActorContext, Job, JobStatus, utc_now
from simon.services.agent_runs import AgentRunService
from simon.services.artifacts import ArtifactStore
from simon.services.canonical import digest

REVIEW_KIND = "platform.artifact_review"
ACCEPTANCE_KIND = "platform.artifact_acceptance"


class ArtifactReviewService:
    def __init__(self, runs: AgentRunService) -> None:
        self.runs, self.store = runs, runs.store

    def get(self, actor: ActorContext, identifier: UUID) -> ArtifactReview:
        job = self.store.get_job(identifier)
        if (
            job is None
            or job.kind != REVIEW_KIND
            or (job.workspace_id, job.created_by) != (actor.workspace_id, actor.actor_id)
        ):
            raise NotFoundError("Artifact review not found")
        review = ArtifactReview.model_validate(job.result)
        self.runs.get(actor, review.run_id)
        return review

    def list(self, actor: ActorContext, run_id: UUID) -> tuple[ArtifactReview, ...]:
        self.runs.get(actor, run_id)
        # Reviews are bounded per run and indexed through its deterministic ledger.
        index = self.store.get_job(uuid5(run_id, "artifact-review-index"))
        return tuple(self.get(actor, UUID(key)) for key in index.input["reviews"]) if index else ()

    def record(
        self, actor: ActorContext, run_id: UUID, request: RecordArtifactReview
    ) -> ArtifactReview:
        self.runs.platform.authorize(actor, write=True)
        run = self.runs.get(actor, run_id)
        plan = self.runs.platform.get(actor, run.plan_id)
        artifact = next(
            (
                item
                for task in run.tasks
                if task.status == "succeeded"
                for item in task.artifacts
                if item.id == request.artifact_id
            ),
            None,
        )
        if artifact is None:
            raise NotFoundError("Completed artifact not found")
        if artifact.sha256 != request.expected_sha256:
            raise InvalidTransitionError("Review references a different artifact revision")
        # Slow file verification stays outside the workspace transaction. Published
        # artifacts are immutable and stored outside worker-writable directories.
        ArtifactStore(self.runs.platform.state_dir / "artifacts").read(artifact)
        identifier = uuid5(run_id, "artifact-review:" + request.idempotency_key)
        payload = request.model_dump(mode="json")
        fingerprint = digest(payload)
        with self.store.transaction(actor.workspace_id):
            self.runs.get(actor, run_id)
            existing = self.store.get_job(identifier)
            if existing is not None:
                if existing.input_digest != fingerprint:
                    raise IdempotencyConflictError(
                        "Review key was already used with different evidence"
                    )
                return self.get(actor, identifier)
            index_id = uuid5(run_id, "artifact-review-index")
            index = self.store.get_job(index_id)
            keys = index.input["reviews"] if index else []
            if len(keys) >= 200:
                raise InvalidTransitionError("Run review limit reached")
            review = ArtifactReview(
                id=identifier,
                run_id=run_id,
                plan_id=plan.id,
                project_id=plan.project_id,
                actor_id=actor.actor_id,
                artifact=artifact,
                delivery_key=request.delivery_key,
                verdict=request.verdict,
                checks=request.checks,
                created_at=utc_now(),
            )
            self.store.create_job(
                Job(
                    id=identifier,
                    workspace_id=actor.workspace_id,
                    created_by=actor.actor_id,
                    kind=REVIEW_KIND,
                    idempotency_key=str(identifier),
                    input=payload,
                    input_digest=fingerprint,
                    status=JobStatus.SUCCEEDED,
                    result=review.model_dump(mode="json"),
                )
            )
            index_payload = {"reviews": [*keys, str(identifier)]}
            if index:
                self.store.save_job(
                    index.model_copy(
                        update={
                            "input": index_payload,
                            "input_digest": digest(index_payload),
                            "updated_at": utc_now(),
                        }
                    ),
                    index.version,
                )
            else:
                self.store.create_job(
                    Job(
                        id=index_id,
                        workspace_id=actor.workspace_id,
                        created_by=actor.actor_id,
                        kind="platform.artifact_review_index",
                        idempotency_key=str(index_id),
                        input=index_payload,
                        input_digest=digest(index_payload),
                        status=JobStatus.SUCCEEDED,
                    )
                )
            self.runs.platform.audit.record(
                event_type="platform.artifact.reviewed",
                actor=actor,
                resource_type=REVIEW_KIND,
                resource_id=str(identifier),
                payload={"artifact_id": str(artifact.id), "verdict": review.verdict},
            )
            return review

    def acceptance(self, actor: ActorContext, review_id: UUID) -> ArtifactAcceptance:
        review = self.get(actor, review_id)
        identifier = uuid5(
            actor.workspace_id,
            f"artifact-acceptance:{actor.actor_id}:{review.project_id or review.plan_id}:"
            + review.delivery_key,
        )
        job = self.store.get_job(identifier)
        if job is None:
            return ArtifactAcceptance(id=identifier, delivery_key=review.delivery_key)
        state = ArtifactAcceptance.model_validate(job.result).model_copy(
            update={"version": job.version}
        )
        for revision in state.revisions:
            self.runs.get(actor, revision.artifact.run_id)
        return state

    def accept(
        self, actor: ActorContext, review_id: UUID, request: AcceptArtifactReview
    ) -> ArtifactAcceptance:
        self.runs.platform.authorize(actor, write=True)
        review = self.get(actor, review_id)
        if review.verdict != "passed":
            raise InvalidTransitionError("Only a passing review can be accepted")
        ArtifactStore(self.runs.platform.state_dir / "artifacts").read(review.artifact)
        with self.store.transaction(actor.workspace_id):
            current = self.acceptance(actor, review_id)
            # Exact retries succeed, but never silently restore a superseded revision.
            if current.revisions and current.revisions[-1].review_id == review_id:
                return current
            latest = next(
                (
                    item
                    for item in reversed(self.list(actor, review.run_id))
                    if item.artifact.id == review.artifact.id
                    and item.delivery_key == review.delivery_key
                ),
                None,
            )
            if latest is None or latest.id != review_id:
                raise InvalidTransitionError("Review evidence changed; use the latest review")
            if current.version != request.expected_version:
                raise InvalidTransitionError("Accepted revision changed; reload before accepting")
            if len(current.revisions) >= 200:
                raise InvalidTransitionError("Deliverable revision limit reached")
            updated = current.model_copy(
                update={
                    "version": current.version + 1,
                    "revisions": (
                        *current.revisions,
                        AcceptedRevision(
                            review_id=review.id,
                            artifact=review.artifact,
                            accepted_at=utc_now(),
                        ),
                    ),
                }
            )
            job = self.store.get_job(current.id)
            if job:
                self.store.save_job(
                    job.model_copy(
                        update={
                            "result": updated.model_dump(mode="json"),
                            "updated_at": utc_now(),
                        }
                    ),
                    job.version,
                )
            else:
                payload = {
                    "delivery_key": review.delivery_key,
                    "scope_id": str(review.project_id or review.plan_id),
                }
                self.store.create_job(
                    Job(
                        id=current.id,
                        workspace_id=actor.workspace_id,
                        created_by=actor.actor_id,
                        kind=ACCEPTANCE_KIND,
                        idempotency_key=str(current.id),
                        input=payload,
                        input_digest=digest(payload),
                        status=JobStatus.SUCCEEDED,
                        result=updated.model_dump(mode="json"),
                    )
                )
            self.runs.platform.audit.record(
                event_type="platform.artifact.accepted",
                actor=actor,
                resource_type=ACCEPTANCE_KIND,
                resource_id=str(current.id),
                payload={"review_id": str(review_id), "version": updated.version},
            )
            return updated
