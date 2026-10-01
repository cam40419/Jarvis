"""Authorized artifact discovery, bounded reads and revision-safe local project copies."""

import base64
import re
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field

from simon.domain.agent_runs import AgentRun, TaskExecution
from simon.domain.artifacts import Artifact
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.models import ActorContext, Job, JobStatus, StrictModel, utc_now
from simon.domain.project_outputs import (
    ProjectOutput,
    ProjectOutputCopy,
    ProjectOutputPage,
    PromoteProjectOutput,
)
from simon.services.agent_runs import AgentRunService
from simon.services.artifacts import ArtifactStore
from simon.services.canonical import digest
from simon.services.local_files import LocalFileService, revision


class _Cursor(StrictModel):
    version: Literal[1] = 1
    project_id: UUID
    actor_id: UUID
    workspace_id: UUID
    created_at: AwareDatetime
    id: UUID
    offset: int = Field(default=0, ge=0, le=4096)


class ProjectOutputService:
    def __init__(
        self, runs: AgentRunService, files: LocalFileService,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self.runs, self.files, self.store = runs, files, runs.store
        self._artifact_store = artifacts

    @property
    def artifacts(self) -> ArtifactStore:
        return self._artifact_store or ArtifactStore(self.runs.platform.state_dir / "artifacts")

    def authorize(self, actor: ActorContext, project_id: UUID, *, write: bool = False) -> None:
        self.runs.platform.authorize(actor, write=write)
        # Includes current membership, scope, household and private-project visibility.
        self.files.connected.identity.membership(actor.actor_id, actor.household_id)
        self.files.connected.projects.project(actor, project_id)

    def promotion_reason(self, actor: ActorContext) -> str | None:
        try:
            self.files.authorize(actor, write=True)
        except AuthorizationError as error:
            return str(error)
        return None

    def _run(self, actor: ActorContext, project_id: UUID, run_id: UUID) -> AgentRun:
        self.authorize(actor, project_id)
        run = self.runs.get(actor, run_id)
        plan = self.runs.platform.get(actor, run.plan_id)
        if plan.project_id != project_id:
            raise NotFoundError("Project output not found")
        return run

    @staticmethod
    def _artifacts(run: AgentRun) -> list[tuple[TaskExecution, Artifact]]:
        return [(task, artifact) for task in run.tasks if task.status == "succeeded"
                for artifact in task.artifacts
                if (artifact.workspace_id, artifact.actor_id, artifact.run_id)
                == (run.workspace_id, run.actor_id, run.id)]

    @staticmethod
    def _copy_id(actor: ActorContext, project_id: UUID, artifact: Artifact) -> UUID:
        return uuid5(NAMESPACE_URL, (
            f"simon:project-output:{actor.household_id}:{actor.actor_id}:"
            f"{project_id}:{artifact.run_id}:{artifact.id}"
        ))

    def _item(
        self, actor: ActorContext, project_id: UUID, run: AgentRun,
        task: TaskExecution, artifact: Artifact,
    ) -> ProjectOutput:
        job = self.store.get_job(self._copy_id(actor, project_id, artifact))
        copied = None
        if job and job.kind == "platform.project_output_copy" and (
            job.household_id, job.created_by
        ) == (actor.household_id, actor.actor_id):
            copied = ProjectOutputCopy.model_validate(job.input["copy"])
        return ProjectOutput(
            id=artifact.id, run_id=run.id, plan_id=run.plan_id, task_id=task.id,
            agent_id=task.agent_id, name=artifact.name, media_type=artifact.media_type,
            size=artifact.size, sha256=artifact.sha256, created_at=artifact.created_at,
            download_url=f"/v1/agent-platform/runs/{run.id}/artifacts/{artifact.id}",
            project_copy=copied,
        )

    def source(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, artifact_id: UUID,
    ) -> tuple[ProjectOutput, Artifact]:
        run = self._run(actor, project_id, run_id)
        for task, artifact in self._artifacts(run):
            if artifact.id == artifact_id:
                return self._item(actor, project_id, run, task, artifact), artifact
        raise NotFoundError("Successful project output not found")

    def read(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, artifact_id: UUID,
    ) -> tuple[ProjectOutput, bytes]:
        item, artifact = self.source(actor, project_id, run_id, artifact_id)
        return item, self.artifacts.read(artifact)

    def current_project(self, actor: ActorContext, run_id: UUID, agent_id: str) -> UUID:
        run = self.runs.get(actor, run_id)
        if agent_id not in {task.agent_id for task in run.tasks}:
            raise AuthorizationError("Agent is not assigned to this run")
        plan = self.runs.platform.get(actor, run.plan_id)
        if plan.project_id is None:
            raise ValidationError("This tool requires a project-assigned run")
        self.authorize(actor, plan.project_id)
        return plan.project_id

    @staticmethod
    def _decode(actor: ActorContext, project_id: UUID, cursor: str | None) -> _Cursor | None:
        if cursor is None:
            return None
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,768}", cursor):
                raise ValueError("Invalid encoding")
            value = _Cursor.model_validate_json(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)),
            )
            if (value.project_id, value.actor_id, value.workspace_id) != (
                project_id, actor.actor_id, actor.household_id,
            ):
                raise ValueError("Cursor owner differs")
            return value
        except ValueError as error:
            raise ValidationError("Invalid project output cursor") from error

    def list(
        self, actor: ActorContext, project_id: UUID, *, limit: int = 20,
        cursor: str | None = None,
    ) -> ProjectOutputPage:
        self.authorize(actor, project_id)
        if not 1 <= limit <= 50:
            raise ValidationError("Project output page size must be between 1 and 50")
        position = self._decode(actor, project_id, cursor)
        before = (position.created_at, position.id) if position else None
        rows = list(self.store.project_run_jobs(
            actor.household_id, actor.actor_id, project_id, before, 51,
        ))
        if position and position.offset:
            anchor = self.store.get_job(position.id)
            if not anchor or anchor.created_at != position.created_at:
                raise ValidationError("Project output cursor no longer exists")
            rows.insert(0, anchor)
        items: list[ProjectOutput] = []
        next_position = None
        # Bound work even when many historical runs have no visible artifacts.
        for index, job in enumerate(rows[:50]):
            start = position.offset if position and position.id == job.id else 0
            try:
                run = self._run(actor, project_id, job.id)
                artifacts = self._artifacts(run)
            except NotFoundError:
                artifacts = []
            for task, artifact in artifacts[start:]:
                items.append(self._item(actor, project_id, run, task, artifact))
                start += 1
                if len(items) == limit:
                    break
            more_in_run = start < len(artifacts)
            if more_in_run or index + 1 < len(rows):
                next_position = _Cursor(
                    project_id=project_id, actor_id=actor.actor_id,
                    workspace_id=actor.household_id, created_at=job.created_at, id=job.id,
                    offset=start if more_in_run else 0,
                )
            else:
                next_position = None
            if len(items) == limit:
                break
        next_cursor = None
        if next_position:
            next_cursor = base64.urlsafe_b64encode(
                next_position.model_dump_json().encode(),
            ).decode().rstrip("=")
        reason = self.promotion_reason(actor)
        return ProjectOutputPage(
            project_id=project_id, items=tuple(items), next_cursor=next_cursor,
            can_promote=reason is None, promotion_blocked_reason=reason,
        )

    def promote(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, artifact_id: UUID,
        request: PromoteProjectOutput, revalidate: Callable[[], ActorContext],
    ) -> ProjectOutput:
        def checked() -> ActorContext:
            current = revalidate()
            if (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id):
                raise AuthorizationError("Project output account changed")
            current = current.model_copy(update={"scopes": current.scopes & actor.scopes})
            self.authorize(current, project_id, write=True)
            self.files.authorize(current, write=True)
            self.source(current, project_id, run_id, artifact_id)
            return current

        current = checked()
        item, data = self.read(current, project_id, run_id, artifact_id)
        path = request.path or f"outputs/{artifact_id.hex[:8]}-{item.name}"
        root = f"project:{project_id}"
        namespace = f"project-output:{actor.household_id}:{actor.actor_id}:{project_id}"
        fingerprint = digest({"run": str(run_id), "artifact": str(artifact_id),
                              "path": path, "revision": request.revision})

        def operation() -> dict[str, Any]:
            current = checked()
            destination = self.files.path(current, root, path)
            # Recognize an already published identical file after a lost DB response.
            # A different file is never overwritten without an explicit revision.
            if not request.revision and destination.exists() and (
                revision(self.files.blob(destination)) == item.sha256
            ):
                saved = {"root": root, "path": path, "revision": item.sha256, "bytes": len(data)}
            else:
                saved = self.files.publish(current, root, path, data, request.revision or "")
            copied = ProjectOutputCopy(
                root=root, path=path, revision=str(saved["revision"]), bytes=len(data),
                saved_at=utc_now(), download_url="/v1/local-files/download?" + urlencode({
                    "root": root, "path": path,
                }),
            )
            _, artifact = self.source(current, project_id, run_id, artifact_id)
            identifier = self._copy_id(current, project_id, artifact)
            values = {"project_id": str(project_id), "run_id": str(run_id),
                      "artifact_id": str(artifact_id), "copy": copied.model_dump(mode="json")}
            existing = self.store.get_job(identifier)
            if existing:
                self.store.save_job(existing.model_copy(update={
                    "input": values, "updated_at": utc_now(),
                }), existing.version)
            else:
                self.store.create_job(Job(
                    id=identifier, household_id=current.household_id, created_by=current.actor_id,
                    kind="platform.project_output_copy", idempotency_key=identifier.hex,
                    input=values, input_digest=digest(values), status=JobStatus.SUCCEEDED,
                ))
            self.files.connected.audit.record(
                actor=current, event_type="project.output_saved", resource_type="project",
                resource_id=str(project_id), payload={
                    "run_id": str(run_id), "artifact_id": str(artifact_id), "path": path,
                    "revision": copied.revision,
                },
            )
            return item.model_copy(update={"project_copy": copied}).model_dump(mode="json")

        with self.store.transaction(actor.household_id):
            result, _ = self.store.execute_once(
                namespace, request.idempotency_key, fingerprint, operation,
            )
        return ProjectOutput.model_validate(result)
