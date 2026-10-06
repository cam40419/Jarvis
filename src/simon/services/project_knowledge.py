"""Project reference memory with scoped, durable search across all activity."""

import base64
import re
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field
from pydantic import ValidationError as PydanticError

from simon.domain.errors import InvalidTransitionError, NotFoundError, ValidationError
from simon.domain.models import ActorContext, Job, JobStatus, StrictModel
from simon.domain.project_knowledge import (
    ActivityKind,
    EditProjectKnowledge,
    ProjectKnowledge,
    ProjectKnowledgePage,
    UpdateProjectKnowledge,
    valid_knowledge_text,
)
from simon.domain.project_work import ProjectActivity, ProjectActivityDraft
from simon.services.canonical import digest
from simon.services.project_work import ProjectWorkService

KNOWLEDGE_KIND = "platform.project_knowledge"
REVISION_KIND = "platform.project_knowledge_revision"


class _HistoryCursor(StrictModel):
    version: Literal[1] = 1
    project_id: UUID
    actor_id: UUID
    workspace_id: UUID
    filter_digest: str = Field(min_length=64, max_length=64)
    before_sequence: int = Field(ge=1)


class ProjectKnowledgeService:
    def __init__(self, work: ProjectWorkService) -> None:
        self.work, self.store = work, work.store

    @staticmethod
    def identifier(actor: ActorContext, project_id: UUID) -> UUID:
        return uuid5(project_id, f"project-knowledge:{actor.workspace_id}:{actor.actor_id}")

    def _job(self, actor: ActorContext, project_id: UUID) -> Job | None:
        self.work.project_resolver(actor, project_id)
        job = self.store.get_job(self.identifier(actor, project_id))
        if job is not None and (job.kind, job.workspace_id, job.created_by) != (
            KNOWLEDGE_KIND,
            actor.workspace_id,
            actor.actor_id,
        ):
            raise NotFoundError("Project knowledge not found")
        return job

    @staticmethod
    def _view(job: Job) -> ProjectKnowledge:
        return ProjectKnowledge.model_validate(
            job.result or job.input["initial_state"],
        ).model_copy(update={"version": job.version})

    def get(self, actor: ActorContext, project_id: UUID) -> ProjectKnowledge:
        self.work.authorize(actor)
        job = self._job(actor, project_id)
        return self._view(job) if job else ProjectKnowledge(project_id=project_id)

    def activity(self, actor: ActorContext, project_id: UUID, identifier: UUID) -> ProjectActivity:
        self.work.authorize(actor)
        self.work.project_resolver(actor, project_id)
        job = self.store.get_job(identifier)
        if job is None or (job.kind, job.workspace_id, job.created_by) != (
            self.work.activity_kind(project_id),
            actor.workspace_id,
            actor.actor_id,
        ):
            raise NotFoundError("Project activity not found")
        return ProjectActivity.model_validate(job.input["initial_state"])

    def update(
        self,
        actor: ActorContext,
        project_id: UUID,
        body: UpdateProjectKnowledge,
        *,
        run_id: UUID | None = None,
        plan_id: UUID | None = None,
        agent_id: str | None = None,
    ) -> ProjectKnowledge:
        self.work.authorize(actor, write=True)
        identifier = self.identifier(actor, project_id)
        with self.store.transaction(actor.workspace_id):
            # Current project authorization applies even to an idempotent response replay.
            self.work.project_resolver(actor, project_id)

            def operation() -> dict[str, object]:
                current = self._job(actor, project_id)
                version = current.version if current else 0
                if body.expected_version != version:
                    raise InvalidTransitionError("Project knowledge changed; reload before saving")
                for decision in body.pinned_decisions:
                    if decision.source_activity_id:
                        self.activity(actor, project_id, decision.source_activity_id)
                state = ProjectKnowledge(
                    project_id=project_id,
                    version=version + 1,
                    brief=body.brief,
                    pinned_decisions=body.pinned_decisions,
                    updated_at=self.work.clock(),
                )
                payload = state.model_dump(mode="json")
                if current:
                    saved = self.store.save_job(
                        current.model_copy(
                            update={
                                "result": payload,
                                "updated_at": self.work.clock(),
                            }
                        ),
                        version,
                    )
                else:
                    saved, _ = self.store.create_job(
                        Job(
                            id=identifier,
                            workspace_id=actor.workspace_id,
                            created_by=actor.actor_id,
                            kind=KNOWLEDGE_KIND,
                            idempotency_key=identifier.hex,
                            input={"project_id": str(project_id), "initial_state": payload},
                            input_digest=digest({"project_id": str(project_id)}),
                            status=JobStatus.SUCCEEDED,
                        )
                    )
                # Keep every prior brief/decision snapshot, including removed pins, for audit.
                revision_id = uuid5(identifier, "revision:" + str(saved.version))
                self.store.create_job(
                    Job(
                        id=revision_id,
                        workspace_id=actor.workspace_id,
                        created_by=actor.actor_id,
                        kind=REVISION_KIND,
                        idempotency_key=revision_id.hex,
                        input={
                            "project_id": str(project_id),
                            "initial_state": payload,
                            "run_id": str(run_id) if run_id else None,
                            "plan_id": str(plan_id) if plan_id else None,
                            "agent_id": agent_id,
                        },
                        input_digest=digest(payload),
                        status=JobStatus.SUCCEEDED,
                    )
                )
                self.work.record_activity(
                    actor,
                    project_id,
                    ProjectActivityDraft(
                        kind="configuration",
                        text=f"Project brief and pinned decisions saved (version {saved.version}; "
                        f"{len(state.pinned_decisions)} pinned decisions).",
                        run_id=run_id,
                        plan_id=plan_id,
                        agent_id=agent_id,
                    ),
                    idempotency_key="knowledge:" + str(saved.version),
                )
                return self._view(saved).model_dump(mode="json")

            result, _ = self.store.execute_once(
                "project-knowledge:" + identifier.hex,
                body.idempotency_key,
                digest(
                    body.model_dump(mode="json", exclude={"idempotency_key"})
                    | (
                        {
                            "run_id": str(run_id) if run_id else None,
                            "plan_id": str(plan_id) if plan_id else None,
                            "agent_id": agent_id,
                        }
                        if run_id is not None or plan_id is not None or agent_id is not None
                        else {}
                    )
                ),
                operation,
            )
            return ProjectKnowledge.model_validate(result)

    def edit(
        self,
        actor: ActorContext,
        project_id: UUID,
        body: EditProjectKnowledge,
        *,
        run_id: UUID | None = None,
        plan_id: UUID | None = None,
        agent_id: str | None = None,
    ) -> ProjectKnowledge:
        """Patch selected fields, retaining the exact receipt on response-loss retries."""
        self.work.authorize(actor, write=True)
        identifier = self.identifier(actor, project_id)
        with self.store.transaction(actor.workspace_id):
            current = self.get(actor, project_id)

            def operation() -> dict[str, object]:
                saved = self.update(
                    actor,
                    project_id,
                    UpdateProjectKnowledge(
                        expected_version=body.expected_version,
                        idempotency_key="edit:" + digest({"key": body.idempotency_key}),
                        brief=current.brief if body.brief is None else body.brief,
                        pinned_decisions=(
                            current.pinned_decisions
                            if body.pinned_decisions is None
                            else body.pinned_decisions
                        ),
                    ),
                    run_id=run_id,
                    plan_id=plan_id,
                    agent_id=agent_id,
                )
                return saved.model_dump(mode="json")

            result, _ = self.store.execute_once(
                "project-knowledge-edit:" + identifier.hex,
                body.idempotency_key,
                digest(
                    {
                        **body.model_dump(mode="json", exclude={"idempotency_key"}),
                        "run_id": str(run_id) if run_id else None,
                        "plan_id": str(plan_id) if plan_id else None,
                        "agent_id": agent_id,
                    }
                ),
                operation,
            )
            return ProjectKnowledge.model_validate(result)

    def history(
        self,
        actor: ActorContext,
        project_id: UUID,
        *,
        query: str = "",
        kind: ActivityKind | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> ProjectKnowledgePage:
        self.work.authorize(actor)
        self.work.project_resolver(actor, project_id)
        if (
            len(query) > 200
            or not 1 <= limit <= 50
            or kind
            not in {
                None,
                "finding",
                "progress",
                "note",
                "decision",
                "blocked",
                "cycle",
                "task",
                "configuration",
            }
        ):
            raise ValidationError("Invalid project knowledge search")
        try:
            valid_knowledge_text(query)
        except ValueError as error:
            raise ValidationError("Invalid project knowledge search text") from error
        query = query.strip()
        filter_digest = digest({"query": query, "kind": kind})
        before = None
        if cursor is not None:
            try:
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", cursor):
                    raise ValueError("Invalid encoding")
                value = _HistoryCursor.model_validate_json(
                    base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)),
                )
                if (value.project_id, value.actor_id, value.workspace_id, value.filter_digest) != (
                    project_id,
                    actor.actor_id,
                    actor.workspace_id,
                    filter_digest,
                ):
                    raise ValueError("Cursor belongs to another project, account or search")
                before = value.before_sequence
            except (ValueError, PydanticError) as error:
                raise ValidationError("Invalid project knowledge cursor") from error
        rows = self.store.project_activity_jobs(
            actor.workspace_id,
            actor.actor_id,
            project_id,
            query,
            kind,
            before,
            limit + 1,
        )
        items = tuple(
            ProjectActivity.model_validate(job.input["initial_state"]) for job in rows[:limit]
        )
        next_cursor = None
        if len(rows) > limit:
            value = _HistoryCursor(
                project_id=project_id,
                actor_id=actor.actor_id,
                workspace_id=actor.workspace_id,
                filter_digest=filter_digest,
                before_sequence=items[-1].sequence,
            )
            next_cursor = base64.urlsafe_b64encode(value.model_dump_json().encode()).decode()
            next_cursor = next_cursor.rstrip("=")
        return ProjectKnowledgePage(project_id=project_id, items=items, next_cursor=next_cursor)
