"""Read-only cleanup preview for verified Simon uploads in current project folders.

Only Drive GETs (plus an in-memory OAuth refresh when necessary) are performed.
No run, project, credential, sync receipt, or remote file is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from simon.adapters.google import GoogleTokens
from simon.adapters.project_drive import FOLDER
from simon.domain.artifacts import Artifact
from simon.domain.models import ActorContext, Channel
from simon.domain.project_files import ProjectDrive, ProjectFileOperation
from simon.services.canonical import digest
from simon.services.identity import ROLE_SCOPES
from simon.services.output_classification import (
    deliverable_filename,
    explicit_copy_filename,
    is_legacy_response_artifact,
    is_response_artifact,
)


@dataclass
class Source:
    id: UUID
    project_id: UUID
    run_id: UUID | None
    task_id: str
    origin: str
    classification: str
    original_name: str
    expected_remote_name: str
    proposed_name: str | None
    media_type: str
    size: int
    sha256: str
    read: Callable[[], bytes]
    copy: dict[str, Any] | None = None

    def operation_id(self, actor: ActorContext, binding: ProjectDrive) -> UUID:
        key = f"{self.origin}:{self.id}:{binding.folder_id}:{binding.google_email}"
        return uuid5(NAMESPACE_URL, f"project-file:{actor.workspace_id}:{actor.actor_id}:{key}")


def verify_copy(
    source: Source,
    actor: ActorContext,
    binding: ProjectDrive,
    operation: ProjectFileOperation,
    original: bytes,
    before: dict[str, Any],
    remote: bytes,
    after: dict[str, Any],
) -> dict[str, Any]:
    """A proposal is possible only when durable provenance and every comparison agree."""
    expected_request = digest(
        {
            "project_id": str(source.project_id),
            "kind": "create",
            "data": {
                "name": source.expected_remote_name,
                "parent": binding.folder_id,
                "mime": source.media_type,
                "sha256": source.sha256,
            },
        }
    )
    saved = operation.result
    expected_id = operation.file_id
    stable_fields = (
        "id",
        "name",
        "mimeType",
        "parents",
        "version",
        "modifiedTime",
        "size",
        "md5Checksum",
        "trashed",
        "_etag",
    )
    checks = {
        "binding": binding.enabled
        and bool(binding.folder_id)
        and bool(binding.google_email)
        and (binding.project_id, binding.actor_id, binding.workspace_id)
        == (source.project_id, actor.actor_id, actor.workspace_id),
        "receipt": operation.id == source.operation_id(actor, binding)
        and (
            operation.project_id,
            operation.actor_id,
            operation.workspace_id,
            operation.google_email,
        )
        == (source.project_id, actor.actor_id, actor.workspace_id, binding.google_email)
        and operation.kind == "create"
        and operation.status == "succeeded"
        and operation.request_digest == expected_request,
        "source_integrity": len(original) == source.size
        and hashlib.sha256(original).hexdigest() == source.sha256,
        "remote_identity": bool(expected_id)
        and saved.get("id") == expected_id == before.get("id") == after.get("id"),
        "not_directory": saved.get("mimeType")
        == before.get("mimeType")
        == source.media_type
        != FOLDER,
        "not_trashed": not before.get("trashed") and not after.get("trashed"),
        "name_unchanged": saved.get("name") == source.expected_remote_name == before.get("name"),
        "parent_unchanged": saved.get("parents") == [binding.folder_id] == before.get("parents"),
        "version_unchanged": bool(saved.get("version"))
        and str(saved.get("version")) == str(before.get("version")),
        "modified_time_unchanged": bool(saved.get("modifiedTime"))
        and saved.get("modifiedTime") == before.get("modifiedTime"),
        "bytes_unchanged": len(remote) == source.size
        and hashlib.sha256(remote).hexdigest() == source.sha256,
        "metadata_size": str(before.get("size")) == str(saved.get("size")) == str(source.size),
        "metadata_checksum": saved.get("md5Checksum")
        == before.get("md5Checksum")
        == hashlib.md5(original, usedforsecurity=False).hexdigest(),
        "stable_during_read": all(before.get(field) == after.get(field) for field in stable_fields),
        "conditional_write_available": bool(after.get("_etag")),
    }
    # Drive versions include invisible server-side changes. Original-version
    # differences are recorded; exact current version is fenced during execution.
    # Some v3 metadata responses have no ETag: use the existing service revision
    # contract rather than inventing a conditional-write token.
    informational = {"version_unchanged", "conditional_write_available"}
    required_failures = [
        name for name, okay in checks.items() if not okay and name not in informational
    ]
    verified = not required_failures
    action, reason = "skip", "verification_failed"
    if verified:
        if source.classification in {"internal_response", "internal_candidate"}:
            action, reason = "trash", "unchanged_internal_response_copy"
        elif (
            source.classification in {"deliverable", "promoted_deliverable"}
            and source.proposed_name
        ):
            action = "keep" if source.proposed_name == before.get("name") else "rename"
            reason = "verified_deliverable"
        else:
            reason = "classification_requires_review"
    return {
        "project_id": str(source.project_id),
        "artifact_id": str(source.id),
        "run_id": str(source.run_id) if source.run_id else None,
        "task_id": source.task_id,
        "origin": source.origin,
        "classification": source.classification,
        "source": {
            "name": source.original_name,
            "sha256": source.sha256,
            "bytes": source.size,
            "media_type": source.media_type,
            "project_copy": source.copy,
        },
        "binding": {
            "project_id": str(binding.project_id),
            "folder_id": binding.folder_id,
            "google_email": binding.google_email,
            "version": binding.version,
        },
        "receipt": {
            "id": str(operation.id),
            "request_digest": operation.request_digest,
            "created_at": operation.created_at.isoformat(),
            "remote_id": expected_id,
            "expected_name": source.expected_remote_name,
            "original_version": saved.get("version"),
            "original_modified_time": saved.get("modifiedTime"),
        },
        "remote": {key: after.get(key) for key in stable_fields},
        "remote_sha256": hashlib.sha256(remote).hexdigest(),
        "checks": checks,
        "changed_or_unverified": [name for name, okay in checks.items() if not okay],
        "required_failed_checks": required_failures,
        "revision_policy": "recheck-preview-current-version-before-standard-service-write",
        "verified_unedited": verified,
        "action": action,
        "reason": reason,
        "proposed_name": source.proposed_name if action in {"rename", "keep"} else None,
    }


def sources(container: Any, actor: ActorContext, project_id: UUID) -> Iterator[Source]:
    before = None
    seen: set[UUID] = set()
    for _ in range(100):
        jobs = container.store.project_run_jobs(
            actor.workspace_id, actor.actor_id, project_id, before, 100
        )
        for job in jobs:
            run = container.project_outputs._run(actor, project_id, job.id)
            tasks = {task.id: task for task in run.tasks}
            rows = [(task, artifact, False) for task in run.tasks for artifact in task.artifacts]
            rows.extend(
                (tasks[entry.task_id], entry.artifact, True)
                for entry in container.project_outputs.journal.list_run(actor, project_id, run.id)
                if entry.kind == "candidate" and entry.task_id in tasks
            )
            for task, artifact, candidate in rows:
                if artifact.id in seen:
                    continue
                seen.add(artifact.id)
                if (artifact.actor_id, artifact.workspace_id, artifact.run_id) != (
                    actor.actor_id,
                    actor.workspace_id,
                    run.id,
                ):
                    continue
                classification = (
                    "internal_candidate"
                    if candidate
                    else "internal_response"
                    if is_response_artifact(task, artifact)
                    else "deliverable"
                )
                proposed_name = (
                    deliverable_filename(artifact.name) if classification == "deliverable" else None
                )
                copied = None
                item = container.project_outputs._item(actor, project_id, run, task, artifact)
                if item.project_copy:
                    copied = item.project_copy.model_dump(mode="json")
                    explicit = explicit_copy_filename(item)
                    if explicit:
                        classification, proposed_name = "promoted_copy_unverified", None
                        try:
                            local = container.connected.local_files
                            content = local.blob(
                                local.path(actor, item.project_copy.root, item.project_copy.path)
                            )
                            if (
                                item.project_copy.root == f"project:{project_id}"
                                and item.project_copy.revision
                                == artifact.sha256
                                == hashlib.sha256(content).hexdigest()
                                and item.project_copy.bytes == len(content) == artifact.size
                            ):
                                classification, proposed_name = "promoted_deliverable", explicit
                        except Exception:
                            pass

                def read_artifact(reference: Artifact = artifact) -> bytes:
                    return bytes(container.project_outputs.artifacts.read(reference))

                yield Source(
                    artifact.id,
                    project_id,
                    run.id,
                    task.id,
                    "agent-output",
                    classification,
                    artifact.name,
                    f"{str(artifact.id)[:8]}-{artifact.name}"[:200],
                    proposed_name,
                    artifact.media_type,
                    artifact.size,
                    artifact.sha256,
                    read_artifact,
                    copied,
                )
        if len(jobs) < 100:
            break
        before = jobs[-1].created_at, jobs[-1].id
    else:
        raise RuntimeError("Run enumeration exceeded its safe bound")
    for offset in range(0, 10000, 100):
        artifacts = container.store.project_artifacts(
            actor.workspace_id, actor.actor_id, project_id, offset, 100
        )
        for artifact in artifacts:
            if (artifact.actor_id, artifact.workspace_id, artifact.project_id) != (
                actor.actor_id,
                actor.workspace_id,
                project_id,
            ):
                continue
            internal = is_legacy_response_artifact(artifact)

            def read_legacy(identifier: UUID = artifact.id) -> bytes:
                record = container.store.project_artifact(identifier)
                if record is None:
                    raise ValueError("Original artifact is unavailable")
                return bytes(record[1])

            yield Source(
                artifact.id,
                project_id,
                None,
                str(artifact.task_id),
                "artifact",
                "internal_response" if internal else "deliverable",
                artifact.name,
                f"{str(artifact.task_id)[:8]}-{artifact.name}",
                None if internal else deliverable_filename(artifact.name),
                artifact.media_type,
                artifact.byte_count,
                artifact.sha256,
                read_legacy,
            )
        if len(artifacts) < 100:
            return
    raise RuntimeError("Artifact enumeration exceeded its safe bound")


def main() -> int:
    from simon.api.app import AppContainer
    from simon.config import Settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=UUID)
    parser.add_argument(
        "--output", type=Path, default=Path(".local/drive-output-cleanup-preview.json")
    )
    args = parser.parse_args()
    container = AppContainer(settings=Settings())
    try:
        member = container.identity.membership(
            container.settings.account_admin_actor_id,
            cast(UUID, container.settings.account_workspace_id),
        )
        actor = ActorContext(
            actor_id=member.actor_id,
            workspace_id=member.workspace_id,
            channel=Channel.API,
            scopes=ROLE_SCOPES[member.role],
        )
        if member.role != "owner":
            raise ValueError("Configured operator is not the current owner")
        manifest: dict[str, Any] = {
            "version": 1,
            "mode": "preview_only",
            "created_at": datetime.now(UTC).isoformat(),
            "actor_id": str(actor.actor_id),
            "workspace_id": str(actor.workspace_id),
            "projects": [],
            "entries": [],
        }
        memories = container.store.explicit_memories(actor.workspace_id, 0, 10000, actor.actor_id)
        projects = [
            item
            for item in memories
            if item.category == "project"
            and item.accepted
            and item.created_by == actor.actor_id
            and (args.project is None or item.id == args.project)
        ]
        token_cache: dict[str, str] = {}
        for project in projects:
            container.connected.projects.project(actor, project.id)
            binding = container.store.project_drive(actor.workspace_id, actor.actor_id, project.id)
            if (
                binding is None
                or not binding.enabled
                or not binding.folder_id
                or not binding.google_email
            ):
                continue
            if binding.google_email not in token_cache:
                connection = container.connected.connection(actor, account=binding.google_email)
                tokens = GoogleTokens.model_validate_json(
                    container.connected.decrypt(connection.encrypted_tokens)
                )
                if tokens.expires_at <= time() + 60:
                    tokens = container.connected.api.refresh(tokens)
                token_cache[binding.google_email] = tokens.access_token
            token = token_cache[binding.google_email]
            api = container.connected.projects.api
            folder = api.metadata(token, binding.folder_id)
            if folder.get("id") != binding.folder_id or folder.get("mimeType") != FOLDER:
                raise ValueError("Linked folder identity changed")
            visible: dict[str, str] = {}
            page_token = ""
            for _ in range(100):
                listing = api.list_files(token, binding.folder_id, page=page_token)
                visible.update({entry["id"]: entry["name"] for entry in listing["files"]})
                page_token = listing["next_page_token"]
                if not page_token:
                    break
            else:
                raise RuntimeError("Folder enumeration exceeded safe bound")
            count_sources = 0
            start = len(manifest["entries"])
            for source in sources(container, actor, project.id):
                count_sources += 1
                operation = container.store.project_file_operation(
                    source.operation_id(actor, binding)
                )
                if operation is None:
                    continue
                minimal = {
                    "project_id": str(project.id),
                    "artifact_id": str(source.id),
                    "classification": source.classification,
                    "receipt_id": str(operation.id),
                    "remote_id": operation.file_id,
                    "action": "skip",
                }
                if operation.status != "succeeded" or not operation.file_id:
                    manifest["entries"].append({**minimal, "reason": "receipt_not_succeeded"})
                    continue
                try:
                    original = source.read()
                    receipt_checks = verify_copy(
                        source, actor, binding, operation, original, {}, b"", {}
                    )["checks"]
                    if not all(
                        receipt_checks[key] for key in ("binding", "receipt", "source_integrity")
                    ):
                        manifest["entries"].append(
                            {**minimal, "reason": "source_or_receipt_provenance_mismatch"}
                        )
                        continue
                    before_metadata = api.metadata(token, operation.file_id)
                    if before_metadata.get("mimeType") == FOLDER:
                        manifest["entries"].append(
                            {**minimal, "reason": "directories_are_never_cleanup_targets"}
                        )
                        continue
                    preliminary = verify_copy(
                        source,
                        actor,
                        binding,
                        operation,
                        original,
                        before_metadata,
                        b"",
                        before_metadata,
                    )
                    download = all(
                        preliminary["checks"][key]
                        for key in (
                            "remote_identity",
                            "not_directory",
                            "not_trashed",
                            "name_unchanged",
                            "parent_unchanged",
                            "modified_time_unchanged",
                            "metadata_size",
                            "metadata_checksum",
                        )
                    )
                    remote = api.download(token, operation.file_id) if download else b""
                    after_metadata = (
                        api.metadata(token, operation.file_id) if download else before_metadata
                    )
                    row = verify_copy(
                        source,
                        actor,
                        binding,
                        operation,
                        original,
                        before_metadata,
                        remote,
                        after_metadata,
                    )
                    row["download_attempted"] = download
                    if row["action"] == "rename" and any(
                        name == row["proposed_name"] and identifier != operation.file_id
                        for identifier, name in visible.items()
                    ):
                        row.update(action="skip", reason="meaningful_name_already_exists")
                    manifest["entries"].append(row)
                except Exception as error:
                    manifest["entries"].append(
                        {
                            **minimal,
                            "reason": "read_or_verification_unavailable",
                            "error_class": type(error).__name__,
                            "http_status": getattr(error, "status", None),
                        }
                    )
            current = container.store.project_drive(actor.workspace_id, actor.actor_id, project.id)
            binding_stable = current is not None and (
                current.enabled,
                current.folder_id,
                current.google_email,
                current.version,
            ) == (binding.enabled, binding.folder_id, binding.google_email, binding.version)
            rows = manifest["entries"][start:]
            if not binding_stable:
                for row in rows:
                    row.update(
                        action="skip",
                        reason="binding_changed_during_preview",
                        verified_unedited=False,
                    )
            summary = {
                "project_id": str(project.id),
                "project_name": project.subject,
                "folder_id": binding.folder_id,
                "binding_version": binding.version,
                "binding_stable": binding_stable,
                "source_count": count_sources,
                "receipt_count": len(rows),
                "folder_item_count": len(visible),
                "actions": dict(Counter(row["action"] for row in rows)),
            }
            manifest["projects"].append(summary)
            print(
                json.dumps(
                    {
                        "project_id": str(project.id),
                        "source_count": count_sources,
                        "receipts_checked": len(rows),
                        "actions": summary["actions"],
                    }
                )
            )
        manifest["totals"] = dict(Counter(row["action"] for row in manifest["entries"]))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "manifest": str(args.output),
                    "projects": len(manifest["projects"]),
                    "totals": manifest["totals"],
                }
            )
        )
        return 0
    except Exception as error:
        print(json.dumps({"preview_failed": True, "error_class": type(error).__name__}))
        return 2
    finally:
        container.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
