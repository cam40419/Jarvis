"""Restartable Drive copies of locally committed, accepted agent outputs."""

from collections.abc import Callable
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from simon.domain.errors import InvalidTransitionError, ValidationError
from simon.domain.models import ActorContext, Job, JobStatus, utc_now
from simon.domain.project_files import ProjectDrive, ProjectFileCreate
from simon.domain.project_outputs import ProjectOutput
from simon.services.canonical import digest
from simon.services.output_classification import (
    receipt_filename,
)
from simon.services.project_files import ProjectFileService
from simon.services.project_outputs import ProjectOutputService


class ProjectOutputReplicationService:
    def __init__(self, outputs: ProjectOutputService, files: ProjectFileService) -> None:
        self.outputs, self.files, self.store = outputs, files, files.store
        self.destination_allowed: Callable[[ActorContext, UUID], bool] | None = None

    @staticmethod
    def identifier(actor: ActorContext, project_id: UUID) -> UUID:
        return uuid5(project_id, f"output-replication:{actor.workspace_id}:{actor.actor_id}")

    @staticmethod
    def destination(binding: ProjectDrive) -> dict[str, Any]:
        return {
            "email": binding.google_email,
            "folder_id": binding.folder_id,
            "binding_version": binding.version,
        }

    def state(self, actor: ActorContext, project_id: UUID) -> dict[str, Any]:
        self.outputs.authorize(actor, project_id)
        binding = self.files.binding(actor, project_id)
        job = self.store.get_job(self.identifier(actor, project_id))
        state = job.result or job.input["initial_state"] if job else {}
        same_destination = state.get("destination") == self.destination(binding)
        return {
            "local_storage": "authoritative",
            "cloud_enabled": binding.enabled
            and (self.destination_allowed is None or self.destination_allowed(actor, project_id)),
            "cloud_status": binding.status,
            "cloud_error": binding.error,
            "last_checked_at": state.get("updated_at") if same_destination else None,
            "copied_outputs": state.get("copied_outputs", 0) if same_destination else 0,
            "pending_scan": bool(state.get("cursor")) if same_destination else binding.enabled,
            "folder_url": self.files.view(actor, binding).get("url"),
        }

    def deliverable_name(
        self, actor: ActorContext, project_id: UUID, item: ProjectOutput
    ) -> str | None:
        """Resolve a real file, including an explicitly named, still-identical local copy.

        Responses remain local history unless separately published to an authored
        filename. An edited or missing copy never authorizes uploading older bytes.
        """
        current, _ = self.outputs.source(actor, project_id, item.run_id, item.id)
        return self.outputs._deliverable_name(actor, project_id, current)

    def _checkpoint(
        self,
        actor: ActorContext,
        project_id: UUID,
        binding: ProjectDrive,
        cursor: str | None,
        receipts: list[UUID],
    ) -> None:
        with self.store.transaction(actor.workspace_id):
            self.files.assert_binding(actor, binding)
            identifier = self.identifier(actor, project_id)
            current = self.store.get_job(identifier)
            old = current.result or current.input["initial_state"] if current else {}
            previous = (
                old.get("copied_outputs", 0)
                if old.get("destination") == self.destination(binding)
                else 0
            )
            copied = 0
            destination_key = digest(self.destination(binding))
            for receipt_id in receipts:
                marker_id = uuid5(identifier, destination_key + ":" + receipt_id.hex)
                if self.store.get_job(marker_id) is not None:
                    continue
                marker = {
                    "project_id": str(project_id),
                    "receipt_id": str(receipt_id),
                    "destination": self.destination(binding),
                }
                self.store.create_job(
                    Job(
                        id=marker_id,
                        workspace_id=actor.workspace_id,
                        created_by=actor.actor_id,
                        kind="platform.output_copy_receipt",
                        idempotency_key=marker_id.hex,
                        input=marker,
                        input_digest=digest(marker),
                        status=JobStatus.SUCCEEDED,
                    )
                )
                copied += 1
            payload = {
                "destination": self.destination(binding),
                "cursor": cursor,
                "copied_outputs": previous + copied,
                "updated_at": utc_now().isoformat(),
            }
            if current:
                self.store.save_job(current.model_copy(update={"result": payload}), current.version)
            else:
                self.store.create_job(
                    Job(
                        id=identifier,
                        workspace_id=actor.workspace_id,
                        created_by=actor.actor_id,
                        kind="platform.output_replication",
                        idempotency_key=identifier.hex,
                        input={"project_id": str(project_id), "initial_state": payload},
                        input_digest=digest(payload),
                        status=JobStatus.SUCCEEDED,
                    )
                )

    def sync(
        self,
        actor: ActorContext,
        project_id: UUID,
        binding: ProjectDrive,
        limit: int,
    ) -> tuple[int, bool]:
        """Called under the existing Drive project's lease; failures never affect local saving.

        Each creation uses a durable provider-generated ID. A lost response is reconciled
        using that ID; existing cloud copies (including subsequent remote edits) are retained.
        The cursor is a scanning checkpoint, not the authoritative copy receipt.
        """
        if limit <= 0:
            return 0, True
        if self.destination_allowed is not None and not self.destination_allowed(actor, project_id):
            return 0, True
        self.outputs.authorize(actor, project_id)
        self.files.assert_binding(actor, binding)
        job = self.store.get_job(self.identifier(actor, project_id))
        saved = job.result or job.input["initial_state"] if job else {}
        cursor = (
            saved.get("cursor") if saved.get("destination") == self.destination(binding) else None
        )
        uploaded = 0
        for _ in range(10):
            page = self.outputs.list(actor, project_id, limit=limit - uploaded, cursor=cursor)
            page_uploaded = 0
            receipts: list[UUID] = []
            for artifact in page.items:
                name = self.deliverable_name(actor, project_id, artifact)
                if name is None:
                    continue
                key = f"agent-output:{artifact.id}:{binding.folder_id}:{binding.google_email}"
                receipt_id = uuid5(
                    NAMESPACE_URL, f"project-file:{actor.workspace_id}:{actor.actor_id}:{key}"
                )
                receipt = self.store.project_file_operation(receipt_id)
                if receipt and (
                    receipt.workspace_id,
                    receipt.actor_id,
                    receipt.project_id,
                    receipt.google_email,
                ) != (actor.workspace_id, actor.actor_id, project_id, binding.google_email):
                    raise ValidationError("The saved upload belongs to another account or project")
                if receipt and receipt.status == "succeeded":
                    receipts.append(receipt_id)
                    continue
                self.files.assert_binding(actor, binding)
                item, content = self.outputs.read(actor, project_id, artifact.run_id, artifact.id)
                media_type, sha256 = item.media_type, item.sha256
                if receipt is None and self.outputs.documents.name(actor, project_id, item):
                    document, content = self.outputs.documents.materialize(
                        actor, project_id, item.run_id, item.id
                    )
                    name, media_type, sha256 = document.name, document.media_type, document.sha256
                    key = self.outputs.documents.cloud_key(document, binding)
                    receipt_id = uuid5(
                        NAMESPACE_URL, f"project-file:{actor.workspace_id}:{actor.actor_id}:{key}"
                    )
                    receipt = self.store.project_file_operation(receipt_id)
                    if receipt and (
                        receipt.workspace_id,
                        receipt.actor_id,
                        receipt.project_id,
                        receipt.google_email,
                    ) != (actor.workspace_id, actor.actor_id, project_id, binding.google_email):
                        raise ValidationError("The saved document upload belongs to another owner")
                    if receipt and receipt.status == "succeeded":
                        receipts.append(receipt_id)
                        continue
                if receipt:
                    assert binding.folder_id is not None
                    name = receipt_filename(
                        receipt,
                        project_id=project_id,
                        parent=binding.folder_id,
                        media_type=media_type,
                        sha256=sha256,
                        names=(name, f"{str(item.id)[:8]}-{item.name}"[:200])
                        if media_type == item.media_type
                        else (name,),
                    )
                result = self.files.create_file(
                    actor,
                    ProjectFileCreate(project_id=project_id, name=name),
                    key,
                    lambda: self.files.current_actor(actor),
                    raw=content,
                    media_type=media_type,
                    retry_create=True,
                )
                if result["status"] != "succeeded":
                    raise InvalidTransitionError(
                        "Cloud copy is pending; the local output remains saved."
                    )
                page_uploaded += 1
                receipts.append(receipt_id)
            cursor = page.next_cursor
            self._checkpoint(actor, project_id, binding, cursor, receipts)
            uploaded += page_uploaded
            if cursor is None or uploaded >= limit:
                return uploaded, cursor is not None
        return uploaded, cursor is not None
