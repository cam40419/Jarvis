"""Stable-identity project metadata with transactional revisions and invocation receipts."""

from uuid import UUID, uuid5

from simon.domain.context import ExplicitMemory
from simon.domain.errors import AuthorizationError, InvalidTransitionError, NotFoundError
from simon.domain.models import ActorContext, Job, JobStatus
from simon.domain.project_details import ProjectDetails, UpdateProjectDetails
from simon.domain.project_work import ProjectActivityDraft
from simon.services.audit import AuditService
from simon.services.canonical import digest
from simon.services.project_work import ProjectWorkService

DETAILS_KIND = "platform.project_details"
REVISION_KIND = "platform.project_details_revision"


class ProjectDetailsService:
    def __init__(self, work: ProjectWorkService) -> None:
        self.work, self.store = work, work.store
        self.audit = AuditService(self.store)

    @staticmethod
    def identifier(actor: ActorContext, project_id: UUID) -> UUID:
        # Shared projects have one version across all authorized editors.
        return uuid5(project_id, f"project-details:{actor.workspace_id}")

    def _project(
        self, actor: ActorContext, project_id: UUID, *, write: bool = False
    ) -> ExplicitMemory:
        self.work.authorize(actor)
        if "memories:read" not in actor.scopes:
            raise AuthorizationError("Project details require memories:read")
        if write:
            self.work.authorize(actor, write=True)
            if "memories:write" not in actor.scopes:
                raise AuthorizationError("Project details require memories:write")
        self.work.project_resolver(actor, project_id)
        project = self.store.explicit_memory(actor.workspace_id, project_id)
        if (
            project is None
            or not project.accepted
            or project.category != "project"
            or (project.scope == "personal" and project.created_by != actor.actor_id)
        ):
            raise NotFoundError("Project not found")
        if write and project.created_by != actor.actor_id and "memories:manage" not in actor.scopes:
            raise AuthorizationError(
                "Only the creator or a workspace owner can edit project details"
            )
        return project

    def _job(self, actor: ActorContext, project: ExplicitMemory) -> Job | None:
        job = self.store.get_job(self.identifier(actor, project.id))
        if job is not None and (
            job.kind,
            job.workspace_id,
            job.created_by,
            job.input.get("project_id"),
        ) != (DETAILS_KIND, project.workspace_id, project.created_by, str(project.id)):
            raise NotFoundError("Project details not found")
        return job

    @staticmethod
    def _view(project: ExplicitMemory, job: Job | None) -> ProjectDetails:
        return ProjectDetails(
            project_id=project.id,
            name=project.subject,
            description=project.content,
            version=job.version if job else 0,
            updated_at=job.updated_at if job else None,
        )

    def get(self, actor: ActorContext, project_id: UUID) -> ProjectDetails:
        with self.store.transaction(actor.workspace_id):
            project = self._project(actor, project_id)
            return self._view(project, self._job(actor, project))

    def update(
        self,
        actor: ActorContext,
        project_id: UUID,
        body: UpdateProjectDetails,
        *,
        run_id: UUID | None = None,
        plan_id: UUID | None = None,
        agent_id: str | None = None,
    ) -> ProjectDetails:
        identifier = self.identifier(actor, project_id)
        with self.store.transaction(actor.workspace_id):
            # Replays never bypass current visibility or write authority.
            project = self._project(actor, project_id, write=True)

            def operation() -> dict[str, object]:
                current = self._job(actor, project)
                before = self._view(project, current)
                if body.expected_version != before.version:
                    raise InvalidTransitionError(
                        "Project details changed; read them again before editing"
                    )
                name = body.name if body.name is not None else project.subject
                description = body.description if body.description is not None else project.content
                fields = tuple(
                    key
                    for key, changed in (
                        ("name", name != project.subject),
                        ("description", description != project.content),
                    )
                    if changed
                )
                if not fields:
                    return before.model_dump(mode="json")
                updated = self.store.update_project_memory(project, name, description)
                now = self.work.clock()
                state = ProjectDetails(
                    project_id=project_id,
                    name=name,
                    description=description,
                    version=before.version + 1,
                    updated_at=now,
                )
                payload = state.model_dump(mode="json")
                if current is None:
                    saved, _ = self.store.create_job(
                        Job(
                            id=identifier,
                            workspace_id=project.workspace_id,
                            created_by=project.created_by,
                            kind=DETAILS_KIND,
                            idempotency_key=identifier.hex,
                            input={"project_id": str(project_id), "initial_state": payload},
                            input_digest=digest({"project_id": str(project_id)}),
                            status=JobStatus.SUCCEEDED,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    saved = self.store.save_job(
                        current.model_copy(
                            update={"result": payload, "updated_at": state.updated_at}
                        ),
                        before.version,
                    )
                after = self._view(updated, saved)
                provenance = {
                    "run_id": str(run_id) if run_id else None,
                    "plan_id": str(plan_id) if plan_id else None,
                    "agent_id": agent_id,
                }
                revision_id = uuid5(identifier, "revision:" + str(saved.version))
                revision = {
                    "project_id": str(project_id),
                    "before": before.model_dump(mode="json"),
                    "after": after.model_dump(mode="json"),
                    **provenance,
                }
                self.store.create_job(
                    Job(
                        id=revision_id,
                        workspace_id=actor.workspace_id,
                        created_by=actor.actor_id,
                        kind=REVISION_KIND,
                        idempotency_key=revision_id.hex,
                        input=revision,
                        input_digest=digest(revision),
                        status=JobStatus.SUCCEEDED,
                    )
                )
                self.audit.record(
                    event_type="project.details_updated",
                    actor=actor,
                    resource_type="project",
                    resource_id=str(project_id),
                    payload={"version": saved.version, "fields": list(fields), **provenance},
                )
                self.work.record_activity(
                    actor,
                    project_id,
                    ProjectActivityDraft(
                        kind="configuration",
                        text=(
                            f"Project details updated: {', '.join(fields)} "
                            f"(version {saved.version})."
                        ),
                        run_id=run_id,
                        plan_id=plan_id,
                        agent_id=agent_id,
                    ),
                    idempotency_key="details:" + str(saved.version),
                )
                return after.model_dump(mode="json")

            result, _ = self.store.execute_once(
                f"project-details:{identifier.hex}:{actor.actor_id}",
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
            return ProjectDetails.model_validate(result)
