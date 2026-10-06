"""Apply a reviewed exact cleanup preview through ordinary revision-checked services.

Without --execute this command only prints the preview totals. Files are renamed or
moved to reversible Drive trash; local source files and upload receipts are retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from simon.domain.models import ActorContext, Channel
from simon.domain.project_files import ProjectFileRename, ProjectTrash
from simon.services.identity import ROLE_SCOPES

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.preview_drive_output_cleanup import Source, sources, verify_copy

GUARDED_FIELDS = (
    "id",
    "name",
    "mimeType",
    "parents",
    "version",
    "modifiedTime",
    "size",
    "md5Checksum",
    "trashed",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def apply_entry(
    container: Any, actor: ActorContext, row: dict[str, Any], source: Source, manifest_hash: str
) -> dict[str, Any]:
    action = row.get("action")
    require(
        action in {"trash", "rename"} and row.get("verified_unedited") is True,
        "Preview entry is not an approved exact copy",
    )
    require(not row.get("required_failed_checks"), "Preview contains a failed comparison")
    project_id = UUID(row["project_id"])
    require(
        source.project_id == project_id and str(source.id) == row["artifact_id"],
        "Source identity changed",
    )
    require(
        source.classification == row["classification"]
        and source.sha256 == row["source"]["sha256"]
        and source.size == row["source"]["bytes"]
        and source.original_name == row["source"]["name"]
        and source.origin == row["origin"],
        "Source provenance changed",
    )
    require(
        source.proposed_name == row.get("proposed_name")
        if action == "rename"
        else source.classification in {"internal_response", "internal_candidate"},
        "Deliverable classification changed",
    )
    files = container.connected.projects

    def current_actor() -> ActorContext:
        current = cast(ActorContext, files.current_actor(actor))
        require(
            (current.actor_id, current.workspace_id) == (actor.actor_id, actor.workspace_id),
            "Owner identity changed",
        )
        files.project(current, project_id)
        binding = container.store.project_drive(actor.workspace_id, actor.actor_id, project_id)
        expected = row["binding"]
        require(
            binding is not None
            and binding.enabled
            and binding.folder_id == expected["folder_id"]
            and binding.google_email == expected["google_email"]
            and binding.version == expected["version"],
            "Project binding changed",
        )
        if source.origin == "agent-output":
            copy_id = uuid5(
                NAMESPACE_URL,
                f"simon:project-output:{actor.workspace_id}:{actor.actor_id}:{project_id}:{source.run_id}:{source.id}",
            )
            copy_job = container.store.get_job(copy_id)
            current_copy = copy_job.input.get("copy") if copy_job else None
            require(current_copy == source.copy, "Project publication changed after preview")
            if source.classification == "promoted_deliverable":
                require(current_copy is not None, "Named project publication is missing")
                current_copy = cast(dict[str, Any], current_copy)
                local = container.connected.local_files
                content = local.blob(
                    local.path(current, current_copy["root"], current_copy["path"])
                )
                require(
                    len(content) == source.size
                    and hashlib.sha256(content).hexdigest() == source.sha256,
                    "Named project publication changed",
                )
        return current

    current_actor()
    binding = container.store.project_drive(actor.workspace_id, actor.actor_id, project_id)
    require(binding is not None, "Project binding is unavailable")
    key = f"reviewed-output-cleanup:{manifest_hash}:{source.id}:{action}"
    cleanup_id = uuid5(NAMESPACE_URL, f"project-file:{actor.workspace_id}:{actor.actor_id}:{key}")
    existing = container.store.project_file_operation(cleanup_id)
    if existing:
        require(
            (existing.project_id, existing.actor_id, existing.workspace_id, existing.google_email)
            == (project_id, actor.actor_id, actor.workspace_id, binding.google_email),
            "Cleanup receipt identity changed",
        )
        require(
            existing.kind == action and existing.file_id == row["remote"]["id"],
            "Cleanup receipt operation changed",
        )
        return {
            "artifact_id": str(source.id),
            "action": action,
            "receipt_id": str(existing.id),
            "remote_id": existing.file_id,
            "status": existing.status,
            "replayed_receipt": True,
        }
    operation = container.store.project_file_operation(source.operation_id(actor, binding))
    require(
        operation is not None
        and str(operation.id) == row["receipt"]["id"]
        and operation.request_digest == row["receipt"]["request_digest"],
        "Upload receipt changed",
    )
    token, email = files.access(actor, project_id, account=binding.google_email)
    require(email == binding.google_email, "Google account changed")
    metadata = files.api.metadata(token, operation.file_id)
    require(
        all(metadata.get(field) == row["remote"].get(field) for field in GUARDED_FIELDS),
        "Remote file changed after preview",
    )
    if row["remote"].get("_etag"):
        require(
            metadata.get("_etag") == row["remote"]["_etag"], "Remote conditional revision changed"
        )
    original = source.read()
    remote = files.api.download(token, operation.file_id)
    after = files.api.metadata(token, operation.file_id)
    verified = verify_copy(source, actor, binding, operation, original, metadata, remote, after)
    require(
        verified["verified_unedited"]
        and verified["action"] == action
        and verified["proposed_name"] == row["proposed_name"],
        "File no longer matches the reviewed action",
    )
    require(
        all(after.get(field) == row["remote"].get(field) for field in GUARDED_FIELDS),
        "Remote file changed during verification",
    )
    if action == "rename":
        page = ""
        for _ in range(100):
            listing = files.api.list_files(token, binding.folder_id, page=page)
            require(
                not any(
                    item["id"] != operation.file_id and item["name"] == source.proposed_name
                    for item in listing["files"]
                ),
                "A file already has the proposed name",
            )
            page = listing["next_page_token"]
            if not page:
                break
        else:
            raise ValueError("Folder enumeration exceeded the safe bound")
        result = files.rename(
            actor,
            ProjectFileRename(
                project_id=project_id,
                file_id=operation.file_id,
                name=cast(str, source.proposed_name),
                revision=str(after["version"]),
            ),
            key,
            current_actor,
        )
    else:
        result = files.trash(
            actor,
            ProjectTrash(
                project_id=project_id,
                file_id=operation.file_id,
                account=binding.google_email,
                revision=str(after["version"]),
            ),
            key,
            current_actor,
        )
    return {
        "artifact_id": str(source.id),
        "action": action,
        "receipt_id": result["id"],
        "remote_id": result.get("file_id"),
        "status": result["status"],
        "permanently_deleted": False,
        "name": source.proposed_name if action == "rename" else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path(".local/drive-output-cleanup-preview.json")
    )
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--result", type=Path, default=Path(".local/drive-output-cleanup-result.json")
    )
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    manifest_hash = hashlib.sha256(raw).hexdigest()
    manifest = json.loads(raw)
    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "preview_only",
                    "manifest_sha256": manifest_hash,
                    "totals": manifest.get("totals"),
                }
            )
        )
        return 0
    require(
        args.expected_manifest_sha256 == manifest_hash,
        "Reviewed manifest checksum is required and must match",
    )
    require(
        manifest.get("mode") == "preview_only" and manifest.get("version") == 1,
        "Unsupported preview format",
    )
    from simon.api.app import AppContainer
    from simon.config import Settings

    container = AppContainer(settings=Settings())
    results: list[dict[str, Any]] = []
    try:
        member = container.identity.membership(
            container.settings.account_admin_actor_id,
            cast(UUID, container.settings.account_workspace_id),
        )
        require(member.role == "owner", "Configured operator is not the current owner")
        actor = ActorContext(
            actor_id=member.actor_id,
            workspace_id=member.workspace_id,
            channel=Channel.API,
            scopes=ROLE_SCOPES[member.role],
        )
        require(
            (str(actor.actor_id), str(actor.workspace_id))
            == (manifest["actor_id"], manifest["workspace_id"]),
            "Manifest belongs to another owner",
        )
        indexed: dict[UUID, dict[UUID, Source]] = {}
        for row in manifest["entries"]:
            if row.get("action") not in {"trash", "rename"}:
                continue
            try:
                project = UUID(row["project_id"])
                if project not in indexed:
                    indexed[project] = {
                        item.id: item for item in sources(container, actor, project)
                    }
                source = indexed[project][UUID(row["artifact_id"])]
                result = apply_entry(container, actor, row, source, manifest_hash)
            except Exception as error:
                result = {
                    "artifact_id": row.get("artifact_id"),
                    "action": row.get("action"),
                    "status": "skipped",
                    "error_class": type(error).__name__,
                }
            results.append(result)
            args.result.parent.mkdir(parents=True, exist_ok=True)
            args.result.write_text(
                json.dumps(
                    {
                        "created_at": datetime.now(UTC).isoformat(),
                        "manifest_sha256": manifest_hash,
                        "results": results,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(json.dumps(result))
        print(
            json.dumps(
                {
                    "result_file": str(args.result),
                    "statuses": dict(Counter(item["status"] for item in results)),
                }
            )
        )
        return 0 if all(item["status"] == "succeeded" for item in results) else 2
    finally:
        container.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
