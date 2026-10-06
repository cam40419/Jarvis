"""Authorized, immutable Word derivatives of accepted project report deliverables."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from simon.domain.artifacts import Artifact
from simon.domain.errors import ValidationError
from simon.domain.models import ActorContext, Job, JobStatus
from simon.domain.project_files import ProjectDrive
from simon.domain.project_outputs import ProjectOutput
from simon.services.canonical import digest
from simon.services.document_rendering import DOCX_MEDIA_TYPE, RENDERER_VERSION, render_report

if TYPE_CHECKING:
    from simon.services.project_outputs import ProjectOutputService

MAX_REPORT_SOURCE_BYTES = 1_000_000
DOCUMENT_KIND = "platform.report_document"
TECHNICAL_NAMES = {
    "readme",
    "changelog",
    "changes",
    "license",
    "licence",
    "copying",
    "notice",
    "contributing",
    "code_of_conduct",
    "security",
    "authors",
    "install",
    "todo",
    "agents",
    "skill",
    "requirements",
    "cmakelists",
    "robots",
    "sitemap",
}


def report_document_name(name: str, media_type: str, size: int) -> str | None:
    """Opt prose file formats in; preserve native formats and repository control files."""
    path = PurePosixPath(name)
    if (
        path.suffix.lower() not in {".md", ".txt"}
        or media_type.partition(";")[0].strip().lower()
        not in {"text/plain", "text/markdown", "text/x-markdown"}
        or not 0 < size <= MAX_REPORT_SOURCE_BYTES
        or path.stem.lower().split(".")[0] in TECHNICAL_NAMES
        or path.stem.lower().startswith(("requirements-", "readme-"))
    ):
        return None
    title = re.sub(r"[_-]+", " ", path.stem).strip()
    title = re.sub(r'[/\\:<>|?*"\x00-\x1f\x7f]', " ", title)
    title = " ".join(title.split()).strip(". ") or "Report"
    if title.islower():
        title = title.title()
    return title[:155].rstrip(". ") + ".docx"


class ReportDocumentService:
    def __init__(self, outputs: ProjectOutputService) -> None:
        self.outputs, self.store = outputs, outputs.store

    def name(self, actor: ActorContext, project_id: UUID, item: ProjectOutput) -> str | None:
        name = self.outputs._deliverable_name(actor, project_id, item)
        return report_document_name(name, item.media_type, item.size) if name else None

    @staticmethod
    def cloud_key(document: Artifact, binding: ProjectDrive) -> str:
        return (
            f"report-document:v{RENDERER_VERSION}:{document.id}:"
            f"{binding.folder_id}:{binding.google_email}"
        )

    def materialize(
        self, actor: ActorContext, project_id: UUID, run_id: UUID, artifact_id: UUID
    ) -> tuple[Artifact, bytes]:
        item, source = self.outputs.source(actor, project_id, run_id, artifact_id)
        name = self.name(actor, project_id, item)
        if name is None:
            raise ValidationError("Only accepted prose report deliverables have Word documents")
        context: dict[str, Any] = {
            "project_id": str(project_id),
            "run_id": str(run_id),
            "source_artifact_id": str(artifact_id),
            "source_sha256": source.sha256,
            "source_bytes": source.size,
            "renderer_version": RENDERER_VERSION,
            "document_name": name,
        }
        identifier = uuid5(source.id, "report-document:" + digest(context))
        with self.store.transaction(actor.workspace_id):
            # Current source authority and named-copy integrity are checked even on
            # cache hits; a stale local copy never authorizes an old report upload.
            current, verified_source = self.outputs.source(actor, project_id, run_id, artifact_id)
            if verified_source != source or self.name(actor, project_id, current) != name:
                raise ValidationError("Report source changed before document preparation")
            existing = self.store.get_job(identifier)
            if existing is not None:
                if (
                    existing.kind != DOCUMENT_KIND
                    or existing.status != JobStatus.SUCCEEDED
                    or (existing.workspace_id, existing.created_by)
                    != (actor.workspace_id, actor.actor_id)
                    or existing.input.get("source") != context
                ):
                    raise ValidationError(
                        "Saved report document provenance differs from its source"
                    )
                document = Artifact.model_validate(existing.input["artifact"])
                if (
                    (document.workspace_id, document.actor_id, document.run_id, document.task_id)
                    != (source.workspace_id, source.actor_id, source.run_id, source.task_id)
                    or document.name != name
                    or document.media_type != DOCX_MEDIA_TYPE
                ):
                    raise ValidationError("Saved report document identity is invalid")
                return document, self.outputs.artifacts.read(document)
            original = self.outputs.artifacts.read(source)
            try:
                markdown = original.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValidationError("Report source must be valid UTF-8 text") from None
            project = self.outputs.files.connected.projects.project(actor, project_id)
            title = PurePosixPath(name).stem
            try:
                content = render_report(markdown, title=title, project_name=project.subject)
            except ValueError as error:
                raise ValidationError(str(error)) from None
            # Keep the original task/run and source bytes untouched. This artifact
            # is reached through its provenance job, never appended to an old run.
            document = self.outputs.artifacts.publish_bytes(
                workspace_id=source.workspace_id,
                actor_id=source.actor_id,
                run_id=source.run_id,
                task_id=source.task_id,
                content=content,
                name=name,
                media_type=DOCX_MEDIA_TYPE,
            )
            current, _ = self.outputs.source(actor, project_id, run_id, artifact_id)
            if self.name(actor, project_id, current) != name:
                raise ValidationError("Report source changed during document preparation")
            values = {
                "source": context,
                "artifact": document.model_dump(mode="json"),
                "title": title,
                "project_name": project.subject,
            }
            self.store.create_job(
                Job(
                    id=identifier,
                    workspace_id=actor.workspace_id,
                    created_by=actor.actor_id,
                    kind=DOCUMENT_KIND,
                    idempotency_key=identifier.hex,
                    input=values,
                    input_digest=digest(values),
                    status=JobStatus.SUCCEEDED,
                )
            )
            return document, self.outputs.artifacts.read(document)
