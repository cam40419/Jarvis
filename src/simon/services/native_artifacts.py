"""Atomic candidate publication, independent review and exact-version acceptance."""

import base64
import binascii
import difflib
import hashlib
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4, uuid5

from simon.adapters.model_endpoints import ModelEndpointError
from simon.domain.errors import (
    AuthorizationError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.models import ActorContext, Channel, utc_now
from simon.domain.native_artifact_commands import (
    DecideNativeArtifact,
    ExactArtifactCommand,
    ReviewNativeArtifact,
    UpdateArtifactReviewPolicy,
    UploadNativeArtifact,
)
from simon.domain.native_artifacts import (
    MAX_ARTIFACT_BYTES,
    NativeArtifact,
    NativeArtifactCheck,
    NativeArtifactDecision,
    NativeArtifactReview,
    NativeArtifactRevision,
    NativeReviewPolicy,
)
from simon.domain.native_execution import NativeExecutionRun
from simon.domain.native_intake import MAX_INTAKE_SOURCE_REVISIONS, IntakeSource
from simon.domain.native_models import ModelUsage
from simon.domain.native_projects import CreateNativeTask, NativeCommand, NativeTask, TaskAssignment
from simon.domain.ports import Store
from simon.services.artifact_reviewer import ArtifactReviewer, ArtifactReviewResult
from simon.services.canonical import digest
from simon.services.identity import IDENTITY_LOCK, ROLE_SCOPES
from simon.services.intake_sources import redact
from simon.services.native_dependencies import DependencyAuthority
from simon.services.native_projects import NativeProjectService
from simon.services.project_models import ProjectModelService, ResolvedProjectModel
from simon.services.workflow_executor import PreparedWorkflowStep, WorkflowPlanningError

MAX_REVISIONS = 100
MAX_REVIEW_ATTEMPTS = 100
MAX_REVIEW_TEXT_BYTES = 48_000
MAX_SOURCE_AUTHORITIES = MAX_INTAKE_SOURCE_REVISIONS
_ACCESS_ERRORS = (AuthorizationError, InvalidTransitionError, NotFoundError, ValidationError)


def revision_metadata(revision: NativeArtifactRevision) -> dict[str, Any]:
    return revision.model_dump(mode="json", exclude={"content_base64"})


class NativeArtifactService:
    def __init__(
        self, store: Store, projects: NativeProjectService, models: ProjectModelService
    ) -> None:
        self.store, self.projects, self.models, self.usage = store, projects, models, models.usage
        self.dependencies = DependencyAuthority(store)

    def _access(self, actor: ActorContext, project_id: UUID, *, write: bool = False) -> None:
        if not isinstance(actor, ActorContext) or actor.channel != Channel.API:
            raise AuthorizationError("Artifact review requires a human project session.")
        project = self.projects._project(actor, project_id, write=write, owner=write)
        if write:
            self.projects._active(project)

    def _policy(self, workspace_id: UUID, project_id: UUID) -> NativeReviewPolicy:
        project = self.store.native_project(workspace_id, project_id)
        if project is None:
            raise NotFoundError("Project not found.")
        return self.store.review_policy(workspace_id, project_id) or NativeReviewPolicy(
            workspace_id=workspace_id, project_id=project_id, updated_at=project.created_at
        )

    def _artifact(self, workspace_id: UUID, project_id: UUID, artifact_id: UUID) -> NativeArtifact:
        artifact = self.store.artifact(workspace_id, project_id, artifact_id)
        if artifact is None:
            raise NotFoundError("Artifact not found.")
        return artifact

    def _revision(
        self, artifact: NativeArtifact, revision_id: UUID | None = None
    ) -> NativeArtifactRevision:
        identifier = revision_id or artifact.current_revision_id
        revision = (
            self.store.artifact_revision(artifact.workspace_id, artifact.project_id, identifier)
            if identifier
            else None
        )
        if revision is None or revision.artifact_id != artifact.id:
            raise NotFoundError("Artifact revision not found.")
        return revision

    def _task(self, artifact: NativeArtifact) -> NativeTask:
        task = self.store.native_task(artifact.workspace_id, artifact.project_id, artifact.task_id)
        if task is None:
            raise NotFoundError("Artifact task not found.")
        return task

    @staticmethod
    def _namespace(actor: ActorContext, project_id: UUID) -> str:
        return f"native_artifacts:{actor.workspace_id}:{project_id}:{actor.actor_id}"

    def _once(
        self,
        actor: ActorContext,
        project_id: UUID,
        command: NativeCommand,
        kind: str,
        target: UUID,
        operation: Callable[[], UUID],
    ) -> UUID:
        result, _ = self.store.execute_once(
            self._namespace(actor, project_id),
            command.idempotency_key,
            digest(
                {
                    "kind": kind,
                    "target": str(target),
                    "command": command.model_dump(mode="json", exclude={"idempotency_key"}),
                }
            ),
            lambda: {"kind": kind, "id": str(operation())},
        )
        return UUID(result["id"])

    def operation_receipt(self, actor: ActorContext, project_id: UUID, key: str) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            receipt = self.store.command_receipt(self._namespace(actor, project_id), key)
            if receipt is None:
                raise NotFoundError("Artifact operation not found.")
            return {
                **receipt,
                "artifact_id": receipt.get("id") if receipt.get("kind") == "upload" else None,
            }

    def _audit(self, actor: ActorContext, project_id: UUID, event: str, **details: Any) -> None:
        self.projects.audit.record(
            event_type="native.artifact." + event,
            actor=actor,
            resource_type="native_artifact",
            resource_id=str(details.get("artifact_id", project_id)),
            payload={"project_id": str(project_id), **details},
        )

    def _touch(self, artifact: NativeArtifact, **changes: Any) -> NativeArtifact:
        updated = NativeArtifact.model_validate(
            {
                **artifact.model_dump(),
                **changes,
                "version": artifact.version + 1,
                "updated_at": utc_now(),
            }
        )
        self.store.update_artifact(updated, artifact.version)
        return updated

    def _task_status(self, task: NativeTask, status: str) -> NativeTask:
        updated = NativeTask.model_validate(
            {
                **task.model_dump(),
                "status": status,
                "version": task.version + 1,
                "updated_at": utc_now(),
            }
        )
        self.store.update_native_task(updated, task.version)
        return updated

    def assert_unshared_tasks(self, artifact: NativeArtifact, task: NativeTask) -> None:
        """One output owns each task lifecycle; repair must preserve other approvals."""
        identifiers = {artifact.task_id, task.id}
        if artifact.pending_repair_task_id is not None:
            identifiers.add(artifact.pending_repair_task_id)
        if artifact.current_revision_id is not None:
            identifiers.add(self._revision(artifact).task_id)
        if self.store.artifact_task_conflict(
            artifact.workspace_id, artifact.project_id, artifact.id, tuple(sorted(identifiers))
        ):
            raise InvalidTransitionError(
                "This output shares an original, maker or repair task with another artifact. "
                "Supported review and repair require a separate task for each output."
            )

    def _publish(
        self,
        artifact: NativeArtifact,
        task: NativeTask,
        content: bytes,
        filename: str,
        media_type: str,
        *,
        actor_id: UUID | None = None,
        agent_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> NativeArtifact:
        self.assert_unshared_tasks(artifact, task)
        previous = self._revision(artifact) if artifact.current_revision_id else None
        sequence = previous.sequence + 1 if previous else 1
        if sequence > MAX_REVISIONS:
            raise ValidationError("This artifact's revision capacity has been reached.")
        revision = NativeArtifactRevision(
            id=uuid5(run_id, "artifact-candidate") if run_id else uuid4(),
            workspace_id=artifact.workspace_id,
            project_id=artifact.project_id,
            artifact_id=artifact.id,
            task_id=task.id,
            run_id=run_id,
            sequence=sequence,
            parent_revision_id=previous.id if previous else None,
            filename=filename,
            media_type=media_type,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            content_base64=base64.b64encode(content).decode("ascii"),
            created_by=actor_id,
            created_by_agent_id=agent_id,
        )
        if previous is not None:
            for review in self.store.artifact_reviews(
                artifact.workspace_id, artifact.project_id, previous.id, 0, MAX_REVIEW_ATTEMPTS
            ):
                if review.status == "running":
                    self._save_review(
                        review,
                        status="stale",
                        summary="A new revision superseded this review.",
                        finished_at=utc_now(),
                    )
        self.store.insert_artifact_revision(revision)
        for name, passed, details in (
            ("content_integrity", True, {"sha256": revision.sha256, "size_bytes": len(content)}),
            (
                "nonempty_content",
                bool(content.strip()),
                {"message": "Candidate must contain nonempty content."},
            ),
        ):
            self.store.insert_artifact_check(
                NativeArtifactCheck(
                    workspace_id=artifact.workspace_id,
                    project_id=artifact.project_id,
                    revision_id=revision.id,
                    sha256=revision.sha256,
                    check_name=name,
                    status="passed" if passed else "failed",
                    details=details,
                )
            )
        artifact = self._touch(
            artifact, current_revision_id=revision.id, pending_repair_task_id=None, name=filename
        )
        if task.id != artifact.task_id:
            self._task_status(self._task(artifact), "in_review")
        return artifact

    def publish_candidate(self, run: NativeExecutionRun, text: str) -> NativeArtifact:
        """Called inside the controller's fenced checkpoint transaction, never by a client."""
        existing = self.store.artifact_revision(
            run.workspace_id, run.project_id, uuid5(run.id, "artifact-candidate")
        )
        if existing is not None:
            if existing.sha256 != hashlib.sha256(text.encode("utf-8")).hexdigest():
                raise InvalidTransitionError("Candidate checkpoint content changed.")
            return self._artifact(run.workspace_id, run.project_id, existing.artifact_id)
        task = self.store.native_task(run.workspace_id, run.project_id, run.task_id)
        if task is None:
            raise NotFoundError("Candidate task not found.")
        matches = self.store.artifacts(run.workspace_id, run.project_id, task_id=task.id)
        pending = [a for a in matches if a.pending_repair_task_id == task.id]
        if len(pending) > 1:
            raise InvalidTransitionError("Repair task has conflicting artifact authority.")
        artifact = pending[0] if pending else next(iter(matches), None)
        if artifact is not None and artifact.pending_repair_task_id not in {None, task.id}:
            raise InvalidTransitionError("Execution must complete the linked repair task.")
        if artifact is None:
            artifact = NativeArtifact(
                id=uuid5(task.id, "execution-artifact"),
                workspace_id=run.workspace_id,
                project_id=run.project_id,
                task_id=task.id,
                name="candidate.txt",
                created_by_agent_id=run.agent_id,
            )
            self.store.insert_artifact(artifact)
        return self._publish(
            artifact,
            task,
            text.encode("utf-8"),
            "candidate.txt",
            "text/plain",
            agent_id=run.agent_id,
            run_id=run.id,
        )

    def upload(
        self, actor: ActorContext, project_id: UUID, command: UploadNativeArtifact
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def publish() -> UUID:
                task = self.projects.get_task(actor, project_id, command.task_id)
                self.projects._version(task.version, command.expected_task_version)
                if task.status == "cancelled":
                    raise InvalidTransitionError("Restore the cancelled task before adding output.")
                try:
                    raw = base64.b64decode(command.content_base64, validate=True)
                except (binascii.Error, ValueError):
                    raise ValidationError("Artifact content must be valid base64.") from None
                if (
                    len(raw) > MAX_ARTIFACT_BYTES
                    or base64.b64encode(raw).decode("ascii") != command.content_base64
                ):
                    raise ValidationError(
                        "Artifact content exceeds its limit or encoding is noncanonical."
                    )
                if command.artifact_id:
                    artifact = self._artifact(actor.workspace_id, project_id, command.artifact_id)
                    self.projects._version(artifact.version, command.expected_version)
                    if task.id not in {artifact.task_id, artifact.pending_repair_task_id}:
                        raise InvalidTransitionError(
                            "A revision must belong to the artifact's task or linked repair."
                        )
                    if (
                        artifact.pending_repair_task_id
                        and task.id != artifact.pending_repair_task_id
                    ):
                        raise InvalidTransitionError(
                            "Submit revisions through the linked repair task."
                        )
                else:
                    self.projects._version(0, command.expected_version)
                    if self.store.artifacts(actor.workspace_id, project_id, task_id=task.id):
                        raise InvalidTransitionError(
                            "This task already has an artifact. "
                            "Add a revision to its existing deliverable."
                        )
                    artifact = NativeArtifact(
                        workspace_id=actor.workspace_id,
                        project_id=project_id,
                        task_id=task.id,
                        name=command.filename,
                        created_by=actor.actor_id,
                    )
                    self.store.insert_artifact(artifact)
                self._task_status(task, "in_review")
                artifact = self._publish(
                    artifact,
                    task,
                    raw,
                    command.filename,
                    command.media_type,
                    actor_id=actor.actor_id,
                )
                self._audit(
                    actor,
                    project_id,
                    "revision_saved",
                    artifact_id=str(artifact.id),
                    revision_id=str(artifact.current_revision_id),
                )
                return artifact.id

            identifier = self._once(
                actor,
                project_id,
                command,
                "upload",
                command.artifact_id or command.task_id,
                publish,
            )
            return self.detail(actor, project_id, identifier)

    def update_policy(
        self, actor: ActorContext, project_id: UUID, command: UpdateArtifactReviewPolicy
    ) -> NativeReviewPolicy:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def update() -> UUID:
                current = self._policy(actor.workspace_id, project_id)
                self.projects._version(current.version, command.expected_version)
                if command.reviewer_agent_id:
                    role = self.store.native_agent(
                        actor.workspace_id, project_id, command.reviewer_agent_id
                    )
                    if role is None or role.status != "active":
                        raise ValidationError("Select an active project reviewer role.")
                policy = NativeReviewPolicy(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    version=current.version + 1,
                    **command.model_dump(exclude={"expected_version", "idempotency_key"}),
                )
                self.store.save_review_policy(policy, current.version)
                self._audit(actor, project_id, "policy_changed", policy_version=policy.version)
                return project_id

            self._once(actor, project_id, command, "policy", project_id, update)
            return self._policy(actor.workspace_id, project_id)

    def _exact(
        self,
        actor: ActorContext,
        project_id: UUID,
        artifact_id: UUID,
        command: ExactArtifactCommand,
    ) -> tuple[NativeArtifact, NativeArtifactRevision, NativeTask, NativeReviewPolicy]:
        artifact = self._artifact(actor.workspace_id, project_id, artifact_id)
        self.projects._version(artifact.version, command.expected_version)
        revision = self._revision(artifact)
        task, policy = self._task(artifact), self._policy(actor.workspace_id, project_id)
        self.assert_unshared_tasks(artifact, task)
        self.projects._version(policy.version, command.expected_policy_version)
        self.projects._version(task.version, command.expected_task_version)
        if (revision.id, revision.sha256) != (command.revision_id, command.sha256):
            raise InvalidTransitionError(
                "Review must identify the current revision and exact content hash."
            )
        if artifact.pending_repair_task_id:
            raise InvalidTransitionError(
                "Complete the pending repair before reviewing or accepting."
            )
        if task.status == "cancelled":
            raise InvalidTransitionError("This task is no longer awaiting review.")
        if task.status == "done":
            if self.acceptance_current(artifact):
                raise InvalidTransitionError(
                    "This revision is already accepted under current authority."
                )
            task = self._task_status(task, "in_review")
        return artifact, revision, task, policy

    def _checks_pass(self, revision: NativeArtifactRevision) -> bool:
        checks = self.store.artifact_checks(revision.workspace_id, revision.project_id, revision.id)
        return bool(checks) and all(
            c.sha256 == revision.sha256 and c.status == "passed" for c in checks
        )

    def _text(self, revision: NativeArtifactRevision) -> str:
        if revision.size_bytes > MAX_REVIEW_TEXT_BYTES or not (
            revision.media_type.startswith("text/")
            or revision.media_type in {"application/json", "application/xml"}
        ):
            raise ValidationError(
                "Independent review currently supports UTF-8 text files up to 48,000 bytes. "
                "This file needs a supported specialist review."
            )
        try:
            text = base64.b64decode(revision.content_base64, validate=True).decode("utf-8-sig")
            if "\x00" in text:
                raise ValueError("Text review cannot contain NUL.")
            return text
        except (UnicodeError, ValueError):
            raise ValidationError("The artifact is not valid UTF-8 text.") from None

    @staticmethod
    def _latest_sources(sources: tuple[IntakeSource, ...]) -> list[IntakeSource]:
        latest: dict[str, IntakeSource] = {}
        for source in sources:
            if (
                source.source_key not in latest
                or latest[source.source_key].revision < source.revision
            ):
                latest[source.source_key] = source
        return sorted(latest.values(), key=lambda source: (source.created_at, source.id))

    def source_authority(self, workspace_id: UUID, project_id: UUID) -> dict[str, Any]:
        """Complete authority metadata; bounded excerpts never determine source validity."""
        sources = self.store.native_intake_sources(workspace_id, project_id)
        if len(sources) > MAX_SOURCE_AUTHORITIES:
            raise ValidationError(
                f"Artifact authority supports at most {MAX_SOURCE_AUTHORITIES} "
                "source revision records; the stored inventory exceeds the intake limit."
            )
        latest = self._latest_sources(sources)
        available = [source for source in latest if not source.revoked_at]
        included = [source for source in available if source.extraction_status == "text"][:8]
        included_ids = {source.id for source in included}
        omitted_ids = [str(source.id) for source in available if source.id not in included_ids]
        return {
            "fingerprint": digest(
                [
                    source.model_dump(
                        mode="json",
                        include={
                            "id",
                            "source_key",
                            "revision",
                            "sha256",
                            "extraction_status",
                            "truncated",
                            "revoked_at",
                        },
                    )
                    for source in sorted(sources, key=lambda source: source.id)
                ]
            ),
            "source_ids": [str(source.id) for source in available],
            "total_sources": len(available),
            "source_revision_count": len(sources),
            "included_source_ids": [str(source.id) for source in included],
            "omitted_source_ids": omitted_ids,
            "omitted_count": len(omitted_ids),
            "coverage": "incomplete"
            if omitted_ids or any(s.truncated or len(s.text) > 2000 for s in included)
            else "complete",
        }

    def _sources(self, workspace_id: UUID, project_id: UUID) -> list[dict[str, Any]]:
        return [
            {
                "id": str(s.id),
                "sha256": s.sha256,
                "text": s.text[:2000],
                "truncated": s.truncated or len(s.text) > 2000,
            }
            for s in self._latest_sources(
                self.store.native_intake_sources(workspace_id, project_id)
            )
            if not s.revoked_at and s.extraction_status == "text"
        ][:8]

    def dependency_snapshot(self, revision: NativeArtifactRevision) -> dict[str, Any] | None:
        return self.dependencies.snapshot(revision.workspace_id, revision.project_id, revision.id)

    @staticmethod
    def _complete_dependencies(snapshot: dict[str, Any] | None) -> bool:
        return snapshot is not None and snapshot.get("coverage") == "complete"

    def _revision_source_authority(
        self, revision: NativeArtifactRevision, snapshot: dict[str, Any] | None
    ) -> dict[str, Any]:
        if not self._complete_dependencies(snapshot):
            return self.source_authority(revision.workspace_id, revision.project_id)
        assert snapshot is not None
        identifiers = list(snapshot["source_ids"])
        return {
            "fingerprint": snapshot["fingerprint"],
            "source_ids": identifiers,
            "total_sources": len(identifiers),
            "source_revision_count": len(identifiers),
            "included_source_ids": [],
            "omitted_source_ids": identifiers,
            "omitted_count": len(identifiers),
            "coverage": "incomplete" if identifiers else "complete",
            "scope": "typed_dependencies",
        }

    def _assert_dependencies_applicable(
        self, revision: NativeArtifactRevision, snapshot: dict[str, Any] | None
    ) -> None:
        if snapshot is not None:
            self.dependencies.assert_applicable(
                revision.workspace_id, revision.project_id, revision.id, snapshot
            )

    def disclosure_source_ids(
        self, artifact: NativeArtifact, revision: NativeArtifactRevision
    ) -> set[str]:
        """Known historical evidence stays attached to the immutable output bytes."""
        identifiers: set[str] = set()
        snapshot = self.dependency_snapshot(revision)
        if snapshot is not None:
            identifiers.update(snapshot.get("source_ids", []))
        for review in self.store.artifact_reviews(
            artifact.workspace_id, artifact.project_id, revision.id, 0, MAX_REVIEW_ATTEMPTS
        ):
            identifiers.update(review.context.get("source_authority", {}).get("source_ids", []))
            identifiers.update(source["id"] for source in review.context.get("sources", []))
            identifiers.update(review.context.get("dependency_snapshot", {}).get("source_ids", []))
        for decision in self.store.artifact_decisions(
            artifact.workspace_id, artifact.project_id, revision.id
        ):
            identifiers.update(
                decision.authority_snapshot.get("source_authority", {}).get("source_ids", [])
            )
            identifiers.update(
                decision.authority_snapshot.get("dependency_snapshot", {}).get("source_ids", [])
            )
        return identifiers

    def assert_disclosable_revision(
        self, artifact: NativeArtifact, revision: NativeArtifactRevision
    ) -> None:
        """Retention does not grant disclosure of known unavailable source evidence."""
        self.dependencies.assert_revision_disclosable(
            artifact.workspace_id, artifact.project_id, revision.id
        )
        for review in self.store.artifact_reviews(
            artifact.workspace_id, artifact.project_id, revision.id, 0, MAX_REVIEW_ATTEMPTS
        ):
            if snapshot := review.context.get("dependency_snapshot"):
                self.dependencies.assert_disclosable(
                    artifact.workspace_id, artifact.project_id, snapshot
                )
        for decision in self.store.artifact_decisions(
            artifact.workspace_id, artifact.project_id, revision.id
        ):
            if snapshot := decision.authority_snapshot.get("dependency_snapshot"):
                self.dependencies.assert_disclosable(
                    artifact.workspace_id, artifact.project_id, snapshot
                )
        for source_id in self.disclosure_source_ids(artifact, revision):
            source = self.store.native_intake_source(
                artifact.workspace_id, artifact.project_id, UUID(source_id)
            )
            if source is None or source.revoked_at is not None:
                raise AuthorizationError(
                    "This revision references unavailable or revoked evidence."
                )

    def _disclosure_allowed(
        self, artifact: NativeArtifact, revision: NativeArtifactRevision
    ) -> bool:
        try:
            self.assert_disclosable_revision(artifact, revision)
        except AuthorizationError:
            return False
        return True

    def _task_metadata(
        self, artifact: NativeArtifact, task: NativeTask, include: set[str] | None = None
    ) -> dict[str, Any]:
        value = task.model_dump(mode="json", include=include)
        if any(
            not self._disclosure_allowed(artifact, revision)
            for revision in self.store.artifact_revisions(
                artifact.workspace_id, artifact.project_id, artifact.id, 0, MAX_REVISIONS
            )
        ):
            if "title" in value:
                value["title"] = "Content withheld"
            if "description" in value:
                value["description"] = ""
            value["disclosure_allowed"] = False
        return value

    def _view_task(self, workspace_id: UUID, project_id: UUID, task: NativeTask) -> dict[str, Any]:
        matches = self.store.artifacts(workspace_id, project_id, task_id=task.id)
        value = self._task_metadata(matches[0], task) if matches else task.model_dump(mode="json")
        return {**value, "artifact_id": str(matches[0].id) if matches else None}

    def _owner_authority(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        self._access(actor, project_id, write=True)
        membership = self.projects._membership(actor.workspace_id, actor.actor_id)
        assert membership is not None
        project_member = self.store.native_project_member(
            actor.workspace_id, project_id, actor.actor_id
        )
        return {
            "actor_id": str(actor.actor_id),
            "workspace_role": membership.role,
            "project_membership": project_member.model_dump(mode="json")
            if project_member
            else None,
        }

    def _acceptance_authority(
        self, actor: ActorContext, artifact: NativeArtifact, revision: NativeArtifactRevision
    ) -> dict[str, Any]:
        project = self.store.native_project(artifact.workspace_id, artifact.project_id)
        maker_task = self.store.native_task(
            artifact.workspace_id, artifact.project_id, revision.task_id
        )
        assert project is not None and maker_task is not None
        snapshot = self.dependency_snapshot(revision)
        self._assert_dependencies_applicable(revision, snapshot)
        authority = {
            "project_version": project.version,
            "owner_authority": self._owner_authority(actor, artifact.project_id),
            "source_authority": self._revision_source_authority(revision, snapshot),
            "maker_task_version": maker_task.version,
        }
        if snapshot is not None:
            authority["dependency_snapshot"] = snapshot
        return authority

    def _decision_authority_current(
        self,
        decision: NativeArtifactDecision,
        artifact: NativeArtifact,
        revision: NativeArtifactRevision,
    ) -> bool:
        membership = self.projects._membership(artifact.workspace_id, decision.decided_by)
        if membership is None or not decision.authority_snapshot:
            return False
        actor = ActorContext(
            actor_id=decision.decided_by,
            workspace_id=artifact.workspace_id,
            scopes=ROLE_SCOPES[membership.role],
            channel=Channel.API,
        )
        try:
            self.assert_disclosable_revision(artifact, revision)
            captured = decision.authority_snapshot.get("dependency_snapshot")
            self._assert_dependencies_applicable(revision, captured)
            current = self._acceptance_authority(actor, artifact, revision)
        except _ACCESS_ERRORS:
            return False
        if self._complete_dependencies(captured):
            if not self._complete_dependencies(current.get("dependency_snapshot")):
                return False
            ignored = {"source_authority", "dependency_snapshot"}
            return {k: v for k, v in current.items() if k not in ignored} == {
                k: v for k, v in decision.authority_snapshot.items() if k not in ignored
            }
        return current == decision.authority_snapshot

    def acceptance_current(self, artifact: NativeArtifact) -> bool:
        """Shared package/dependency gate, distinct from historical approval records."""
        return self._status(artifact) == "accepted"

    def _context(
        self,
        artifact: NativeArtifact,
        revision: NativeArtifactRevision,
        task: NativeTask,
        reviewer_id: UUID,
    ) -> dict[str, Any]:
        self.assert_disclosable_revision(artifact, revision)
        project = self.store.native_project(artifact.workspace_id, artifact.project_id)
        reviewer = self.store.native_agent(artifact.workspace_id, artifact.project_id, reviewer_id)
        assert project is not None and reviewer is not None
        maker_task = self.store.native_task(
            artifact.workspace_id, artifact.project_id, revision.task_id
        )
        assert maker_task is not None
        snapshot = self.dependency_snapshot(revision)
        self._assert_dependencies_applicable(revision, snapshot)
        feedback = [
            self._public_decision(artifact, prior, decision)
            for prior in self.store.artifact_revisions(
                artifact.workspace_id, artifact.project_id, artifact.id, 0, MAX_REVISIONS
            )
            for decision in self.store.artifact_decisions(
                artifact.workspace_id, artifact.project_id, prior.id
            )
            if decision.decision == "changes_requested"
        ]
        context = {
            "revision": {**revision_metadata(revision), "text": self._text(revision)},
            "task": self._task_metadata(artifact, task, {"id", "title", "description"}),
            "maker_task": self._task_metadata(
                artifact, maker_task, {"id", "version", "title", "description"}
            ),
            "owner_feedback": [
                {
                    **decision,
                    "feedback": decision["feedback"][:2000],
                    "truncated": len(decision["feedback"]) > 2000,
                }
                for decision in feedback[-3:]
            ],
            "difference": self._difference(artifact, revision),
            "project": {"objective": project.objective},
            "reviewer": reviewer.model_dump(
                mode="json", include={"id", "instructions", "success_criteria"}
            ),
            "sources": []
            if self._complete_dependencies(snapshot)
            else self._sources(artifact.workspace_id, artifact.project_id),
            "source_authority": self._revision_source_authority(revision, snapshot),
            "checks": [
                c.model_dump(mode="json")
                for c in self.store.artifact_checks(
                    artifact.workspace_id, artifact.project_id, revision.id
                )
            ],
        }
        if snapshot is not None:
            context["dependency_snapshot"] = snapshot
            context["review_facts"] = snapshot.get("review_facts", [])

        def clean(value: Any) -> Any:
            if isinstance(value, str):
                return redact(value)[0]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        return clean(context)  # type: ignore[no-any-return]

    def _difference(
        self, artifact: NativeArtifact, revision: NativeArtifactRevision
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "text": "",
            "truncated": False,
            "supported": True,
            "from_revision_id": str(revision.parent_revision_id)
            if revision.parent_revision_id
            else None,
            "to_revision_id": str(revision.id),
        }
        if not self._disclosure_allowed(artifact, revision):
            return {**result, "supported": False, "disclosure_allowed": False}
        if not revision.parent_revision_id:
            return result
        prior = self._revision(artifact, revision.parent_revision_id)
        try:
            self.assert_disclosable_revision(artifact, prior)
            self.assert_disclosable_revision(artifact, revision)
        except AuthorizationError:
            return {**result, "supported": False, "disclosure_allowed": False}
        try:
            old_text, new_text = self._text(prior), self._text(revision)
        except ValidationError:
            return {**result, "supported": False}
        chunks: list[str] = []
        length = 0
        for line in difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"revision-{prior.sequence}",
            tofile=f"revision-{revision.sequence}",
        ):
            if not line.endswith("\n"):
                line += "\n\\ No newline at end of file\n"
            if length + len(line) > 4000:
                chunks.append(line[: 4000 - length])
                result["truncated"] = True
                break
            chunks.append(line)
            length += len(line)
        return {**result, "text": "".join(chunks)}

    def _review_authority(self, review: NativeArtifactReview, *, accepted: bool = False) -> None:
        member = self.projects._membership(review.workspace_id, review.requested_by)
        if member is None:
            raise AuthorizationError("The review's authorizing owner no longer has access.")
        actor = ActorContext(
            actor_id=review.requested_by,
            workspace_id=review.workspace_id,
            scopes=ROLE_SCOPES[member.role],
            channel=Channel.API,
        )
        self._access(actor, review.project_id, write=True)
        artifact = self._artifact(review.workspace_id, review.project_id, review.artifact_id)
        revision, task = self._revision(artifact), self._task(artifact)
        policy = self._policy(review.workspace_id, review.project_id)
        project = self.store.native_project(review.workspace_id, review.project_id)
        reviewer = self.store.native_agent(
            review.workspace_id, review.project_id, review.reviewer_agent_id
        )
        runtime = self.models.runtime(actor, review.project_id)
        binding = runtime.bindings[runtime.review_endpoint_id]
        snapshot = review.context.get("dependency_snapshot")
        self._assert_dependencies_applicable(revision, snapshot)
        sources_changed = False
        if not self._complete_dependencies(snapshot):
            sources = [
                (s["id"], s["sha256"])
                for s in self._sources(review.workspace_id, review.project_id)
            ]
            reviewed_sources = [(s["id"], s["sha256"]) for s in review.context.get("sources", [])]
            sources_changed = (
                sources != reviewed_sources
                or review.context.get("source_authority", {}).get("fingerprint")
                != self.source_authority(review.workspace_id, review.project_id)["fingerprint"]
            )
        maker_task = self.store.native_task(
            review.workspace_id, review.project_id, revision.task_id
        )
        maker_version = review.context.get("maker_task", {}).get("version")
        review_task = (
            self.store.native_task(review.workspace_id, review.project_id, review.review_task_id)
            if review.review_task_id
            else None
        )
        if review.review_task_id and (
            review_task is None
            or review_task.version
            != (review.review_task_version or 0) + int(review.status != "running")
            or review_task.assignment.agent_id != review.reviewer_agent_id
            or review_task.status == "cancelled"
        ):
            raise InvalidTransitionError("The independent review task changed or was cancelled.")
        if (
            artifact.version != review.artifact_version
            or revision.id != review.revision_id
            or revision.sha256 != review.sha256
            or artifact.pending_repair_task_id
            or task.version != review.task_version + int(accepted)
            or policy.version != review.policy_version
            or project is None
            or project.version != review.project_version
            or reviewer is None
            or reviewer.status != "active"
            or reviewer.version != review.reviewer_version
            or binding.model.id != review.model_id
            or binding.model.version != review.model_version
            or binding.fingerprint != review.endpoint_fingerprint
            or sources_changed
            or maker_task is None
            or maker_task.version != maker_version + int(accepted)
        ):
            raise InvalidTransitionError("Artifact review inputs or authority changed.")

    def _save_review(self, review: NativeArtifactReview, **changes: Any) -> NativeArtifactReview:
        updated = NativeArtifactReview.model_validate(
            {**review.model_dump(), **changes, "version": review.version + 1}
        )
        self.store.update_artifact_review(updated, review.version)
        if updated.status != "running" and updated.review_task_id:
            task = self.store.native_task(
                updated.workspace_id, updated.project_id, updated.review_task_id
            )
            if (
                task
                and task.version == updated.review_task_version
                and task.assignment.agent_id == updated.reviewer_agent_id
            ):
                status = (
                    "done"
                    if updated.status == "completed" and updated.verdict == "approved"
                    else "cancelled"
                    if updated.status in {"stale", "cancelled"}
                    else "blocked"
                )
                self._task_status(task, status)
        return updated

    def _public_review(self, review: NativeArtifactReview) -> dict[str, Any]:
        usage = (
            self.store.model_usage(review.workspace_id, review.project_id, review.usage_id)
            if review.usage_id
            else None
        )
        if review.status == "running" and usage and usage.deadline_at <= utc_now():
            self.usage.recover(review.workspace_id)
            review = self._save_review(
                review,
                status="unknown",
                summary="Review dispatch was interrupted.",
                finished_at=utc_now(),
            )
            usage = self.store.model_usage(review.workspace_id, review.project_id, usage.id)
        value = review.model_dump(mode="json", exclude={"context"})
        value["source_authority"] = review.context.get("source_authority")
        artifact = self._artifact(review.workspace_id, review.project_id, review.artifact_id)
        revision = self._revision(artifact, review.revision_id)
        value["disclosure_allowed"] = self._disclosure_allowed(artifact, revision)
        value["dependency_snapshot"] = (
            review.context.get("dependency_snapshot") if value["disclosure_allowed"] else None
        )
        if not value["disclosure_allowed"]:
            value.update(summary="", findings=[])
        value["usage"] = (
            usage.model_dump(mode="json", exclude={"endpoint_snapshot"}) if usage else None
        )
        return value

    def unresolved_review_usage(self, artifact: NativeArtifact) -> bool:
        """Historical unknown effects remain actionable after a new head or decision."""
        for revision in self.store.artifact_revisions(
            artifact.workspace_id, artifact.project_id, artifact.id, 0, MAX_REVISIONS
        ):
            for review in self.store.artifact_reviews(
                artifact.workspace_id, artifact.project_id, revision.id, 0, MAX_REVIEW_ATTEMPTS
            ):
                usage = (
                    self.store.model_usage(
                        artifact.workspace_id, artifact.project_id, review.usage_id
                    )
                    if review.usage_id
                    else None
                )
                if usage and usage.status == "unknown":
                    return True
                if review.status == "unknown" and (
                    usage is None or usage.status in {"reserved", "dispatched"}
                ):
                    return True
        return False

    def _review_capacity(self, artifact: NativeArtifact, policy: NativeReviewPolicy) -> None:
        """Reserve human attention before provider dispatch, under the workspace lock."""
        outputs = self.store.artifacts(artifact.workspace_id, artifact.project_id, limit=1001)
        if len(outputs) > 1000:
            raise ValidationError("Review admission supports at most 1,000 project outputs.")
        pending = 0
        for other in outputs:
            if other.id == artifact.id or other.current_revision_id is None:
                continue
            status = self._status(other)
            needs_repair_decision = (
                status == "changes_requested" and not other.pending_repair_task_id
            )
            if (
                status in {"reviewing", "awaiting_approval"}
                or needs_repair_decision
                or self.unresolved_review_usage(other)
            ):
                pending += 1
                if pending >= policy.max_pending_reviews:
                    raise InvalidTransitionError(
                        "The pending review limit has been reached. Resolve an existing "
                        "decision or uncertain review before starting another provider call."
                    )

    def review(
        self,
        actor: ActorContext,
        project_id: UUID,
        artifact_id: UUID,
        command: ReviewNativeArtifact,
    ) -> dict[str, Any]:
        recovery_epoch = self.usage.recovery.assert_active()
        admitted = False
        prepared: PreparedWorkflowStep | None = None
        reviewer_client: ArtifactReviewer | None = None
        binding: ResolvedProjectModel | None = None
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def admit() -> UUID:
                nonlocal admitted, prepared, reviewer_client, binding
                artifact, revision, task, policy = self._exact(
                    actor, project_id, artifact_id, command
                )
                self._review_capacity(artifact, policy)
                if not self._checks_pass(revision):
                    raise InvalidTransitionError("Deterministic artifact checks failed.")
                attempts = self.store.artifact_reviews(
                    actor.workspace_id, project_id, revision.id, 0, MAX_REVIEW_ATTEMPTS
                )
                total_attempts = sum(
                    len(
                        self.store.artifact_reviews(
                            actor.workspace_id, project_id, r.id, 0, MAX_REVIEW_ATTEMPTS
                        )
                    )
                    for r in self.store.artifact_revisions(
                        actor.workspace_id, project_id, artifact.id, 0, MAX_REVISIONS
                    )
                )
                if total_attempts >= MAX_REVIEW_ATTEMPTS:
                    raise ValidationError("This artifact's review capacity has been reached.")
                for attempt in attempts:
                    if attempt.status == "running":
                        usage = (
                            self.store.model_usage(actor.workspace_id, project_id, attempt.usage_id)
                            if attempt.usage_id
                            else None
                        )
                        if usage and usage.deadline_at <= utc_now():
                            self.usage.recover(actor.workspace_id)
                            self._save_review(
                                attempt,
                                status="unknown",
                                summary=(
                                    "Review dispatch was interrupted; usage remains recorded. "
                                    "Reconcile usage before starting another review."
                                ),
                                finished_at=utc_now(),
                            )
                        else:
                            raise InvalidTransitionError(
                                "An independent review is already running."
                            )
                if self.unresolved_review_usage(artifact):
                    raise InvalidTransitionError(
                        "This artifact has an unresolved review effect; reconcile its usage "
                        "before starting another concurrent review."
                    )
                reviewer_id = command.reviewer_agent_id or policy.reviewer_agent_id
                reviewer = (
                    self.store.native_agent(actor.workspace_id, project_id, reviewer_id)
                    if reviewer_id
                    else None
                )
                maker_ids = {
                    revision.created_by_agent_id,
                    artifact.created_by_agent_id,
                    task.assignment.agent_id,
                }
                if reviewer is None or reviewer.status != "active" or reviewer.id in maker_ids:
                    raise ValidationError("Choose an active reviewer role separate from the maker.")
                if policy.reviewer_agent_id and reviewer.id != policy.reviewer_agent_id:
                    raise ValidationError("Use the reviewer role selected by project policy.")
                runtime = self.models.runtime(actor, project_id)
                binding = runtime.bindings[runtime.review_endpoint_id]
                reviewer_client = ArtifactReviewer(binding, transport=self.models.transport)
                context = self._context(artifact, revision, task, reviewer.id)
                try:
                    prepared = reviewer_client.prepare(context)
                except WorkflowPlanningError as exc:
                    message = {
                        "review_context_limit_exceeded": (
                            "The artifact and evidence exceed the review context limit. "
                            "Reduce the artifact, brief, or source excerpts before reviewing."
                        ),
                        "review_endpoint_unavailable": (
                            "The selected review model cannot handle this review context. "
                            "Choose a qualified review model with enough context capacity."
                        ),
                        "review_pricing_unavailable": (
                            "The selected review model needs configured pricing before review."
                        ),
                    }.get(exc.code, "The selected model cannot prepare this artifact review.")
                    raise ValidationError(message) from None
                project = self.store.native_project(actor.workspace_id, project_id)
                assert project is not None
                review_task = self.projects.create_task(
                    actor,
                    project_id,
                    CreateNativeTask(
                        title=("Review: " + task.title)[:200],
                        description=(
                            f"Independently review artifact {artifact.id}, "
                            f"revision {revision.id}, SHA-256 {revision.sha256}.\n"
                            f"Original brief: {task.description[:4000]}"
                        ),
                        assignment=TaskAssignment(kind="agent", agent_id=reviewer.id),
                        idempotency_key="artifact-review-task-" + digest(command.idempotency_key),
                    ),
                )
                review_task = self._task_status(review_task, "in_progress")
                review = NativeArtifactReview(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    artifact_id=artifact.id,
                    revision_id=revision.id,
                    sha256=revision.sha256,
                    artifact_version=artifact.version,
                    policy_version=policy.version,
                    task_version=task.version,
                    project_version=project.version,
                    reviewer_agent_id=reviewer.id,
                    reviewer_version=reviewer.version,
                    review_task_id=review_task.id,
                    review_task_version=review_task.version,
                    model_id=binding.model.id,
                    model_version=binding.model.version,
                    endpoint_fingerprint=binding.fingerprint,
                    requested_by=actor.actor_id,
                    idempotency_key=command.idempotency_key,
                    request_digest=digest(
                        command.model_dump(mode="json", exclude={"idempotency_key"})
                    ),
                    context=context,
                )
                now = utc_now()
                usage = ModelUsage(
                    workspace_id=actor.workspace_id,
                    project_id=project_id,
                    operation_id=review.id,
                    phase="artifact_review",
                    requested_by=actor.actor_id,
                    model_id=binding.model.id,
                    model_version=binding.model.version,
                    credential_revision=binding.model.credential_revision,
                    template_id=binding.model.template_id,
                    model=binding.endpoint.model,
                    endpoint_fingerprint=binding.fingerprint,
                    endpoint_snapshot=binding.endpoint.model_dump(mode="json"),
                    input_rate=prepared.input_rate,
                    output_rate=prepared.output_rate,
                    reserved_microusd=prepared.reservation_microusd,
                    held_microusd=prepared.reservation_microusd,
                    started_at=now,
                    deadline_at=now + timedelta(seconds=300),
                )
                self.usage.reserve(actor, project_id, review.id, (usage,))
                self.models.assert_current(actor, project_id, binding)
                self.usage.dispatch(actor, project_id, usage.id)
                review = NativeArtifactReview.model_validate(
                    {**review.model_dump(), "usage_id": usage.id}
                )
                self.store.insert_artifact_review(review)
                self._audit(
                    actor,
                    project_id,
                    "review_dispatched",
                    artifact_id=str(artifact.id),
                    review_id=str(review.id),
                    revision_id=str(revision.id),
                )
                admitted = True
                return review.id

            review_id = self._once(actor, project_id, command, "review", artifact_id, admit)
            review = self.store.artifact_review(actor.workspace_id, project_id, review_id)
            assert review is not None
            if not admitted:
                return self._public_review(review)
        # Dispatch is reserved and durable. Never retry the same call after a lost response.
        result = None
        output: ArtifactReviewResult | None = None
        error, unknown = None, False
        assert reviewer_client is not None and prepared is not None
        try:
            self.usage.recovery.assert_epoch(recovery_epoch)
            output, result = reviewer_client.generate(prepared)
            if (output.revision_id, output.sha256) != (review.revision_id, review.sha256):
                error = "review_revision_mismatch"
        except WorkflowPlanningError as exc:
            result, error = exc.result, exc.code
            unknown = exc.may_have_been_dispatched and result is None
        except ModelEndpointError as exc:
            error, unknown = exc.code, exc.may_have_been_dispatched
        except Exception:
            # Retain uncertainty and accounting if dispatch was interrupted unexpectedly.
            error, unknown = "artifact_review_interrupted", True
        assert review.usage_id is not None
        settled = self.usage.settle(
            review.workspace_id,
            project_id,
            review.usage_id,
            result,
            unknown=unknown,
            error_code=error,
        )
        if settled.status == "unknown":
            unknown, error = True, error or "review_usage_unknown"
        if settled.error_code == "provider_usage_exceeded_reservation":
            error = settled.error_code
        with (
            self.store.transaction(IDENTITY_LOCK),
            self.store.transaction(actor.workspace_id),
            self.usage.recovery.operation(),
        ):
            self.usage.recovery.assert_epoch(recovery_epoch)
            current = self.store.artifact_review(actor.workspace_id, project_id, review.id)
            assert current is not None
            if current.status != "running":
                self._access(actor, project_id, write=True)
                return self._public_review(current)
            stale = False
            try:
                self._review_authority(current)
            except _ACCESS_ERRORS:
                stale = True
            status = (
                "stale" if stale else "unknown" if unknown else "failed" if error else "completed"
            )
            current = self._save_review(
                current,
                status=status,
                verdict=output.verdict if status == "completed" and output else None,
                summary=output.summary
                if status == "completed" and output
                else error or "Review authority changed before completion.",
                findings=output.findings if status == "completed" and output else (),
                finished_at=utc_now(),
            )
            if (
                current.status == "completed"
                and current.verdict == "approved"
                and not self._policy(actor.workspace_id, project_id).require_human_approval
            ):
                artifact = self._artifact(actor.workspace_id, project_id, artifact_id)
                self._approve(actor, artifact, self._revision(artifact), current, automatic=True)
            public = self._public_review(current)
        # Commit settlement and stale-result fencing before enforcing response access.
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)
            return public

    def _approved_review(
        self, artifact: NativeArtifact, revision: NativeArtifactRevision, *, accepted: bool = False
    ) -> NativeArtifactReview | None:
        for review in reversed(
            self.store.artifact_reviews(
                artifact.workspace_id, artifact.project_id, revision.id, 0, MAX_REVIEW_ATTEMPTS
            )
        ):
            try:
                self._review_authority(review, accepted=accepted)
            except _ACCESS_ERRORS:
                continue
            return review if review.status == "completed" and review.verdict == "approved" else None
        return None

    def _approve(
        self,
        actor: ActorContext,
        artifact: NativeArtifact,
        revision: NativeArtifactRevision,
        review: NativeArtifactReview | None,
        *,
        automatic: bool = False,
        feedback: str = "",
    ) -> NativeArtifactDecision:
        task = self._task_status(self._task(artifact), "done")
        if revision.task_id != task.id:
            maker_task = self.store.native_task(
                artifact.workspace_id, artifact.project_id, revision.task_id
            )
            assert maker_task is not None
            self._task_status(maker_task, "done")
        decision = NativeArtifactDecision(
            workspace_id=artifact.workspace_id,
            project_id=artifact.project_id,
            artifact_id=artifact.id,
            revision_id=revision.id,
            sha256=revision.sha256,
            policy_version=self._policy(artifact.workspace_id, artifact.project_id).version,
            task_version=task.version,
            review_id=review.id if review else None,
            decided_by=actor.actor_id,
            decision="approved",
            feedback=feedback,
            automatic=automatic,
            authority_snapshot=self._acceptance_authority(actor, artifact, revision),
        )
        self.store.insert_artifact_decision(decision)
        self._audit(
            actor,
            artifact.project_id,
            "accepted",
            artifact_id=str(artifact.id),
            revision_id=str(revision.id),
            decision_id=str(decision.id),
            automatic=automatic,
        )
        return decision

    def decide(
        self,
        actor: ActorContext,
        project_id: UUID,
        artifact_id: UUID,
        command: DecideNativeArtifact,
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id, write=True)

            def apply() -> UUID:
                artifact, revision, task, policy = self._exact(
                    actor, project_id, artifact_id, command
                )
                if command.decision == "approved":
                    self.assert_disclosable_revision(artifact, revision)
                    if not self._checks_pass(revision):
                        raise InvalidTransitionError(
                            "Deterministic artifact checks must pass before acceptance."
                        )
                    review = self._approved_review(artifact, revision)
                    if policy.require_agent_review and review is None:
                        raise InvalidTransitionError(
                            "A current independent review must approve this revision first."
                        )
                    self._approve(actor, artifact, revision, review, feedback=command.feedback)
                else:
                    decisions = [
                        decision
                        for rev in self.store.artifact_revisions(
                            actor.workspace_id, project_id, artifact.id, 0, MAX_REVISIONS
                        )
                        for decision in self.store.artifact_decisions(
                            actor.workspace_id, project_id, rev.id
                        )
                    ]
                    if (
                        sum(d.decision == "changes_requested" for d in decisions)
                        >= policy.max_repair_cycles
                    ):
                        raise InvalidTransitionError(
                            "The configured repair limit has been reached. "
                            "Revise policy deliberately before continuing."
                        )
                    assignment = task.assignment
                    try:
                        self.projects._assignment(actor, project_id, assignment)
                    except InvalidTransitionError:
                        assignment = TaskAssignment()
                    repair_brief = (
                        task.description[:1800]
                        if self._disclosure_allowed(artifact, revision)
                        else "Prior evidence is unavailable. Use current permitted sources."
                    )
                    repair = self.projects.create_task(
                        actor,
                        project_id,
                        CreateNativeTask(
                            title=("Revise: " + task.title)[:200],
                            description=(
                                f"Repair artifact {artifact.id}, revision {revision.id}, "
                                f"SHA-256 {revision.sha256}.\n"
                                f"Original brief: {repair_brief}\n"
                                f"Owner feedback: {command.feedback}"
                            )[:8000],
                            assignment=assignment,
                            idempotency_key="artifact-repair-" + digest(command.idempotency_key),
                        ),
                    )
                    task = self._task_status(task, "blocked")
                    self._touch(artifact, pending_repair_task_id=repair.id)
                    decision = NativeArtifactDecision(
                        workspace_id=actor.workspace_id,
                        project_id=project_id,
                        artifact_id=artifact.id,
                        revision_id=revision.id,
                        sha256=revision.sha256,
                        policy_version=policy.version,
                        task_version=task.version,
                        repair_task_id=repair.id,
                        decided_by=actor.actor_id,
                        decision="changes_requested",
                        feedback=command.feedback,
                    )
                    self.store.insert_artifact_decision(decision)
                    self._audit(
                        actor,
                        project_id,
                        "repair_requested",
                        artifact_id=str(artifact.id),
                        revision_id=str(revision.id),
                        repair_task_id=str(repair.id),
                    )
                return artifact.id

            self._once(actor, project_id, command, "decision", artifact_id, apply)
            return self.detail(actor, project_id, artifact_id)

    def _status(self, artifact: NativeArtifact) -> str:
        revision = self._revision(artifact)
        for attempt in self.store.artifact_reviews(
            artifact.workspace_id, artifact.project_id, revision.id, 0, MAX_REVIEW_ATTEMPTS
        ):
            if attempt.status == "running":
                self._public_review(attempt)
        if artifact.pending_repair_task_id:
            return "changes_requested"
        if not self._checks_pass(revision):
            return "validation_failed"
        try:
            snapshot = self.dependency_snapshot(revision)
            self._assert_dependencies_applicable(revision, snapshot)
        except _ACCESS_ERRORS:
            return "stale"
        policy, task = (
            self._policy(artifact.workspace_id, artifact.project_id),
            self._task(artifact),
        )
        decisions = self.store.artifact_decisions(
            artifact.workspace_id, artifact.project_id, revision.id
        )
        if decisions:
            decision = decisions[-1]
            captured = decision.authority_snapshot.get("dependency_snapshot")
            try:
                self._assert_dependencies_applicable(revision, captured)
            except _ACCESS_ERRORS:
                return "stale"
            if (
                decision.decision == "approved"
                and decision.policy_version == policy.version
                and decision.task_version == task.version
                and task.status == "done"
                and self._decision_authority_current(decision, artifact, revision)
                and (
                    not policy.require_agent_review
                    or self._approved_review(artifact, revision, accepted=True)
                )
            ):
                return "accepted"
        review = self._approved_review(artifact, revision)
        if review:
            return "awaiting_approval"
        reviews = self.store.artifact_reviews(
            artifact.workspace_id, artifact.project_id, revision.id, 0, MAX_REVIEW_ATTEMPTS
        )
        if reviews and (captured := reviews[-1].context.get("dependency_snapshot")):
            try:
                self._assert_dependencies_applicable(revision, captured)
            except _ACCESS_ERRORS:
                return "stale"
        if any(r.status == "running" for r in reviews):
            return "reviewing"
        if (
            reviews
            and reviews[-1].status == "completed"
            and reviews[-1].verdict == "changes_requested"
        ):
            return "changes_requested"
        return "awaiting_review"

    def _public_decision(
        self,
        artifact: NativeArtifact,
        revision: NativeArtifactRevision,
        decision: NativeArtifactDecision,
    ) -> dict[str, Any]:
        value = decision.model_dump(mode="json")
        value["disclosure_allowed"] = self._disclosure_allowed(artifact, revision)
        if not value["disclosure_allowed"]:
            value["feedback"] = ""
            value["authority_snapshot"] = {
                key: item
                for key, item in value["authority_snapshot"].items()
                if key != "dependency_snapshot"
            }
        return value

    def view(
        self, actor: ActorContext, project_id: UUID, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            self.projects._page(offset, limit)
            can_manage = False
            try:
                self._access(actor, project_id, write=True)
                can_manage = True
            except _ACCESS_ERRORS:
                pass
            artifacts = self.store.artifacts(
                actor.workspace_id, project_id, offset=offset, limit=limit
            )
            return {
                "policy": self._policy(actor.workspace_id, project_id).model_dump(mode="json"),
                "can_manage": can_manage,
                "artifacts": {
                    "items": [
                        {**a.model_dump(mode="json"), "status": self._status(a)} for a in artifacts
                    ],
                    "has_more": bool(
                        self.store.artifacts(
                            actor.workspace_id, project_id, offset=offset + limit, limit=1
                        )
                    ),
                    "offset": offset,
                    "limit": limit,
                },
                "tasks": [
                    self._view_task(actor.workspace_id, project_id, t)
                    for t in self.store.native_tasks(actor.workspace_id, project_id, 0, 100)
                ],
                "agents": [
                    a.model_dump(mode="json")
                    for a in self.store.native_agents(actor.workspace_id, project_id, 0, 100)
                ],
            }

    def detail(self, actor: ActorContext, project_id: UUID, artifact_id: UUID) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            artifact = self._artifact(actor.workspace_id, project_id, artifact_id)
            revision = self._revision(artifact)
            revisions = self.store.artifact_revisions(
                actor.workspace_id, project_id, artifact_id, 0, MAX_REVISIONS
            )
            can_manage = False
            try:
                self._access(actor, project_id, write=True)
                can_manage = True
            except _ACCESS_ERRORS:
                pass
            disclosure_allowed = self._disclosure_allowed(artifact, revision)
            try:
                dependencies = self.dependency_snapshot(revision)
            except _ACCESS_ERRORS:
                dependencies = None
            return {
                "artifact": artifact.model_dump(mode="json"),
                "status": self._status(artifact),
                "current_revision_id": str(revision.id),
                "policy": self._policy(actor.workspace_id, project_id).model_dump(mode="json"),
                "task": self._task_metadata(artifact, self._task(artifact)),
                "repair_task": self._task_metadata(artifact, repair)
                if artifact.pending_repair_task_id
                and (
                    repair := self.store.native_task(
                        actor.workspace_id, project_id, artifact.pending_repair_task_id
                    )
                )
                else None,
                "maker_task": self._task_metadata(artifact, maker)
                if (
                    maker := self.store.native_task(
                        actor.workspace_id, project_id, revision.task_id
                    )
                )
                else None,
                "difference": self._difference(artifact, revision),
                "disclosure_allowed": disclosure_allowed,
                "dependency_snapshot": dependencies if disclosure_allowed else None,
                "can_manage": can_manage,
                "revisions": [revision_metadata(r) for r in revisions],
                "checks": [
                    c.model_dump(mode="json")
                    for r in revisions
                    for c in self.store.artifact_checks(actor.workspace_id, project_id, r.id)
                ],
                "reviews": [
                    self._public_review(review)
                    for r in revisions
                    for review in self.store.artifact_reviews(
                        actor.workspace_id, project_id, r.id, 0, MAX_REVIEW_ATTEMPTS
                    )
                ],
                "decisions": [
                    self._public_decision(artifact, r, d)
                    for r in revisions
                    for d in self.store.artifact_decisions(actor.workspace_id, project_id, r.id)
                ],
            }

    def read_content(
        self, actor: ActorContext, project_id: UUID, artifact_id: UUID, revision_id: UUID
    ) -> tuple[NativeArtifactRevision, bytes]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            self._access(actor, project_id)
            revision = self._revision(
                self._artifact(actor.workspace_id, project_id, artifact_id), revision_id
            )
            self.assert_disclosable_revision(
                self._artifact(actor.workspace_id, project_id, artifact_id), revision
            )
            # Revalidate persisted bytes at the read boundary; corrupt content is never served.
            revision = NativeArtifactRevision.model_validate(revision.model_dump())
            return revision, base64.b64decode(revision.content_base64, validate=True)

    def preview(
        self, actor: ActorContext, project_id: UUID, artifact_id: UUID, revision_id: UUID
    ) -> dict[str, Any]:
        with self.store.transaction(IDENTITY_LOCK), self.store.transaction(actor.workspace_id):
            revision, _raw = self.read_content(actor, project_id, artifact_id, revision_id)
            try:
                text: str | None = self._text(revision)
            except ValidationError:
                text = None
            return {"revision_id": str(revision.id), "sha256": revision.sha256, "text": text}
