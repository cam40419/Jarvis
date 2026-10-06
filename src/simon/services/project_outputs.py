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
from simon.domain.run_journal import JournalEntry
from simon.services.agent_runs import AgentRunService
from simon.services.artifacts import ArtifactStore
from simon.services.canonical import digest
from simon.services.local_files import LocalFileService, revision
from simon.services.output_classification import (
    deliverable_filename,
    explicit_copy_filename,
    is_response_artifact,
)
from simon.services.report_documents import ReportDocumentService
from simon.services.run_journal import RunJournalService


class _Cursor(StrictModel):
    version: Literal[1] = 1
    project_id: UUID
    actor_id: UUID
    workspace_id: UUID
    created_at: AwareDatetime
    id: UUID
    offset: int = Field(default=0, ge=0, le=4096)
    kind: Literal["response", "deliverable"] | None = None


class ProjectOutputService:
    def __init__(
        self,
        runs: AgentRunService,
        files: LocalFileService,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self.runs, self.files, self.store = runs, files, runs.store
        self._artifact_store = artifacts
        self.journal = RunJournalService(runs, artifacts=artifacts)
        self.documents = ReportDocumentService(self)

    @property
    def artifacts(self) -> ArtifactStore:
        return self._artifact_store or ArtifactStore(self.runs.platform.state_dir / "artifacts")

    def authorize(self, actor: ActorContext, project_id: UUID, *, write: bool = False) -> None:
        self.runs.platform.authorize(actor, write=write)
        # Includes current membership, scope, workspace and private-project visibility.
        self.files.connected.identity.membership(actor.actor_id, actor.workspace_id)
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
        return [
            (task, artifact)
            for task in run.tasks
            if task.status == "succeeded"
            for artifact in task.artifacts
            if (artifact.workspace_id, artifact.actor_id, artifact.run_id)
            == (run.workspace_id, run.actor_id, run.id)
        ]

    @staticmethod
    def _copy_id(actor: ActorContext, project_id: UUID, artifact: Artifact) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            (
                f"simon:project-output:{actor.workspace_id}:{actor.actor_id}:"
                f"{project_id}:{artifact.run_id}:{artifact.id}"
            ),
        )

    def _saved_copy(
        self, actor: ActorContext, project_id: UUID, artifact: Artifact
    ) -> ProjectOutputCopy | None:
        job = self.store.get_job(self._copy_id(actor, project_id, artifact))
        if (
            job
            and job.kind == "platform.project_output_copy"
            and (job.workspace_id, job.created_by) == (actor.workspace_id, actor.actor_id)
        ):
            return ProjectOutputCopy.model_validate(job.input["copy"])
        return None

    def _item(
        self,
        actor: ActorContext,
        project_id: UUID,
        run: AgentRun,
        task: TaskExecution,
        artifact: Artifact,
        candidate: JournalEntry | None = None,
    ) -> ProjectOutput:
        copied = self._saved_copy(actor, project_id, artifact)
        status: Literal["accepted", "draft", "partial"] = "accepted"
        if candidate:
            status = (
                "accepted"
                if candidate.status == "accepted"
                else (
                    "partial"
                    if candidate.status == "partial"
                    or task.status in {"failed", "cancelled", "blocked", "unknown"}
                    else "draft"
                )
            )
        base = f"/v1/projects/{project_id}/outputs/{run.id}/{artifact.id}"
        item = ProjectOutput(
            id=artifact.id,
            run_id=run.id,
            plan_id=run.plan_id,
            task_id=task.id,
            agent_id=task.agent_id,
            name=artifact.name,
            media_type=artifact.media_type,
            size=artifact.size,
            sha256=artifact.sha256,
            created_at=artifact.created_at,
            download_url=base + "/download"
            if candidate
            else f"/v1/agent-platform/runs/{run.id}/artifacts/{artifact.id}",
            project_copy=copied,
            title=candidate.title
            if candidate
            else (
                task.id.replace("_", " ").replace("-", " ").capitalize()
                if is_response_artifact(task, artifact)
                else artifact.name
            ),
            status=status,
            task_status=task.status,
            source="candidate" if candidate else "artifact",
            kind="response" if candidate or is_response_artifact(task, artifact) else "deliverable",
            step=candidate.step if candidate else None,
            preview_url=base + "/preview",
        )
        document_name = self.documents.name(actor, project_id, item)
        return item.model_copy(
            update={
                "document_name": document_name,
                "document_url": base + "/document" if document_name else None,
            }
        )

    def _deliverable_name(
        self, actor: ActorContext, project_id: UUID, item: ProjectOutput
    ) -> str | None:
        """Resolve an already-authorized item without recursively loading its source."""
        if (
            item.status != "accepted"
            or item.task_status != "succeeded"
            or item.task_id == "lead-plan"
        ):
            return None
        if item.kind == "deliverable":
            return deliverable_filename(item.name)
        name, copied = explicit_copy_filename(item), item.project_copy
        if (
            name is None
            or copied is None
            or copied.root != f"project:{project_id}"
            or copied.revision != item.sha256
            or copied.bytes != item.size
        ):
            return None
        try:
            self.files.authorize(actor)
            content = self.files.blob(self.files.path(actor, copied.root, copied.path))
        except (OSError, ValidationError, AuthorizationError):
            return None
        return (
            name if len(content) == copied.bytes and revision(content) == copied.revision else None
        )

    def _output_rows(
        self, actor: ActorContext, project_id: UUID, run: AgentRun
    ) -> list[tuple[TaskExecution, Artifact, JournalEntry | None]]:
        tasks = {task.id: task for task in run.tasks}
        candidates = [
            item
            for item in self.journal.list_run(actor, project_id, run.id)
            if item.kind == "candidate" and item.task_id in tasks
        ]
        # Keep candidate revisions in append order. A final answer with the same
        # bytes is already represented; workspace exports remain separate outputs.
        represented = {(item.task_id, item.artifact.sha256) for item in candidates}
        rows: list[tuple[TaskExecution, Artifact, JournalEntry | None]] = [
            (tasks[item.task_id], item.artifact, item) for item in candidates
        ]
        rows.extend(
            (task, artifact, None)
            for task, artifact in self._artifacts(run)
            if not is_response_artifact(task, artifact)
            or (task.id, artifact.sha256) not in represented
            or self._saved_copy(actor, project_id, artifact) is not None
        )
        return rows

    def source(
        self,
        actor: ActorContext,
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
    ) -> tuple[ProjectOutput, Artifact]:
        run = self._run(actor, project_id, run_id)
        for task, artifact, candidate in self._output_rows(actor, project_id, run):
            if artifact.id == artifact_id:
                return self._item(actor, project_id, run, task, artifact, candidate), artifact
        # Preserve existing answer artifact IDs even when the journal has the same
        # content; earlier plans/dependency receipts may already reference them.
        for task, artifact in self._artifacts(run):
            if artifact.id == artifact_id:
                return self._item(actor, project_id, run, task, artifact), artifact
        raise NotFoundError("Project output not found")

    def read(
        self,
        actor: ActorContext,
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
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
    def _decode(
        actor: ActorContext,
        project_id: UUID,
        cursor: str | None,
        kind: Literal["response", "deliverable"] | None = None,
    ) -> _Cursor | None:
        if cursor is None:
            return None
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,768}", cursor):
                raise ValueError("Invalid encoding")
            value = _Cursor.model_validate_json(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)),
            )
            if (value.project_id, value.actor_id, value.workspace_id) != (
                project_id,
                actor.actor_id,
                actor.workspace_id,
            ):
                raise ValueError("Cursor owner differs")
            if value.kind != kind:
                raise ValueError("Cursor output kind differs")
            return value
        except ValueError as error:
            raise ValidationError("Invalid project output cursor") from error

    def list(
        self,
        actor: ActorContext,
        project_id: UUID,
        *,
        limit: int = 20,
        cursor: str | None = None,
        kind: Literal["response", "deliverable"] | None = None,
    ) -> ProjectOutputPage:
        self.authorize(actor, project_id)
        if not 1 <= limit <= 50:
            raise ValidationError("Project output page size must be between 1 and 50")
        if kind not in {None, "response", "deliverable"}:
            raise ValidationError("Unknown project output kind")
        position = self._decode(actor, project_id, cursor, kind)
        before = (position.created_at, position.id) if position else None
        rows = list(
            self.store.project_run_jobs(
                actor.workspace_id,
                actor.actor_id,
                project_id,
                before,
                51,
            )
        )
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
                artifacts = self._output_rows(actor, project_id, run)
            except NotFoundError:
                artifacts = []
            for task, artifact, candidate in artifacts[start:]:
                start += 1
                item = self._item(actor, project_id, run, task, artifact, candidate)
                if (
                    kind
                    and item.kind != kind
                    and not (kind == "deliverable" and item.document_url is not None)
                ):
                    continue
                items.append(item)
                if len(items) == limit:
                    break
            more_in_run = start < len(artifacts)
            if more_in_run or index + 1 < len(rows):
                next_position = _Cursor(
                    project_id=project_id,
                    actor_id=actor.actor_id,
                    workspace_id=actor.workspace_id,
                    created_at=job.created_at,
                    id=job.id,
                    offset=start if more_in_run else 0,
                    kind=kind,
                )
            else:
                next_position = None
            if len(items) == limit:
                break
        next_cursor = None
        if next_position:
            next_cursor = (
                base64.urlsafe_b64encode(
                    next_position.model_dump_json().encode(),
                )
                .decode()
                .rstrip("=")
            )
        reason = self.promotion_reason(actor)
        return ProjectOutputPage(
            project_id=project_id,
            items=tuple(items),
            next_cursor=next_cursor,
            can_promote=reason is None,
            promotion_blocked_reason=reason,
        )

    def promote(
        self,
        actor: ActorContext,
        project_id: UUID,
        run_id: UUID,
        artifact_id: UUID,
        request: PromoteProjectOutput,
        revalidate: Callable[[], ActorContext],
    ) -> ProjectOutput:
        def checked() -> ActorContext:
            current = revalidate()
            if (current.actor_id, current.workspace_id) != (actor.actor_id, actor.workspace_id):
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
        namespace = f"project-output:{actor.workspace_id}:{actor.actor_id}:{project_id}"
        fingerprint = digest(
            {
                "run": str(run_id),
                "artifact": str(artifact_id),
                "path": path,
                "revision": request.revision,
            }
        )

        def operation() -> dict[str, Any]:
            current = checked()
            destination = self.files.path(current, root, path)
            # Recognize an already published identical file after a lost DB response.
            # A different file is never overwritten without an explicit revision.
            if (
                not request.revision
                and destination.exists()
                and (revision(self.files.blob(destination)) == item.sha256)
            ):
                saved = {"root": root, "path": path, "revision": item.sha256, "bytes": len(data)}
            else:
                saved = self.files.publish(current, root, path, data, request.revision or "")
            copied = ProjectOutputCopy(
                root=root,
                path=path,
                revision=str(saved["revision"]),
                bytes=len(data),
                saved_at=utc_now(),
                download_url="/v1/local-files/download?"
                + urlencode(
                    {
                        "root": root,
                        "path": path,
                    }
                ),
            )
            _, artifact = self.source(current, project_id, run_id, artifact_id)
            identifier = self._copy_id(current, project_id, artifact)
            values = {
                "project_id": str(project_id),
                "run_id": str(run_id),
                "artifact_id": str(artifact_id),
                "copy": copied.model_dump(mode="json"),
            }
            existing = self.store.get_job(identifier)
            if existing:
                self.store.save_job(
                    existing.model_copy(
                        update={
                            "input": values,
                            "updated_at": utc_now(),
                        }
                    ),
                    existing.version,
                )
            else:
                self.store.create_job(
                    Job(
                        id=identifier,
                        workspace_id=current.workspace_id,
                        created_by=current.actor_id,
                        kind="platform.project_output_copy",
                        idempotency_key=identifier.hex,
                        input=values,
                        input_digest=digest(values),
                        status=JobStatus.SUCCEEDED,
                    )
                )
            self.files.connected.audit.record(
                actor=current,
                event_type="project.output_saved",
                resource_type="project",
                resource_id=str(project_id),
                payload={
                    "run_id": str(run_id),
                    "artifact_id": str(artifact_id),
                    "path": path,
                    "revision": copied.revision,
                },
            )
            published = item.model_copy(update={"project_copy": copied})
            document_name = self.documents.name(current, project_id, published)
            return published.model_copy(
                update={
                    "document_name": document_name,
                    "document_url": (
                        f"/v1/projects/{project_id}/outputs/{run_id}/{artifact_id}/document"
                    )
                    if document_name
                    else None,
                }
            ).model_dump(mode="json")

        with self.store.transaction(actor.workspace_id):
            result, _ = self.store.execute_once(
                namespace,
                request.idempotency_key,
                fingerprint,
                operation,
            )
        return ProjectOutput.model_validate(result)
