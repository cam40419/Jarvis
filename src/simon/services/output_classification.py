"""Classify controller responses by their provenance, never their prose."""

import hashlib
import re
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, UUID, uuid5

from simon.domain.agent_runs import TaskExecution
from simon.domain.artifacts import Artifact
from simon.domain.errors import ValidationError
from simon.domain.project_files import ProjectFileOperation
from simon.domain.project_outputs import ProjectOutput
from simon.domain.tasks import ProjectArtifact
from simon.services.canonical import digest


def is_response_artifact(task: TaskExecution, artifact: Artifact) -> bool:
    return bool(
        task.artifacts
        and task.artifacts[0].id == artifact.id
        and artifact.name in {"answer.txt", "answer.json"}
        and artifact.sha256 == hashlib.sha256(task.output.encode()).hexdigest()
    )


def is_legacy_response_artifact(artifact: ProjectArtifact) -> bool:
    return artifact.name == "result.md" and artifact.id == uuid5(
        NAMESPACE_URL, f"simon:task-artifact:{artifact.task_id}:result.md"
    )


def deliverable_filename(name: str) -> str:
    """Drive receives the authored basename, without generated identity prefixes."""
    basename = PurePosixPath(name.replace("\\", "/")).name
    if (
        basename in {"", ".", ".."}
        or not basename.strip()
        or any(character in basename for character in ("\0", "\r", "\n"))
    ):
        raise ValidationError("The deliverable needs a valid filename")
    if len(basename) > 200:
        suffix = PurePosixPath(basename).suffix[:30]
        basename = basename[: 200 - len(suffix)] + suffix
    return basename


def explicit_copy_filename(item: ProjectOutput) -> str | None:
    """A named local publication can be a deliverable even when its source was an answer."""
    if item.project_copy is None:
        return None
    name = deliverable_filename(item.project_copy.path)
    if re.fullmatch(
        r"(?:[0-9a-f]{8}-)?(?:answer\.(?:txt|json|md)|result\.md|candidate-\d+\.(?:md|txt|json))",
        name,
        flags=re.IGNORECASE,
    ):
        return None
    return name


def receipt_filename(
    operation: ProjectFileOperation,
    *,
    project_id: UUID,
    parent: str,
    media_type: str,
    sha256: str,
    names: tuple[str, ...],
) -> str:
    """Recover the exact old create name before reconciling its existing provider ID."""
    if operation.kind != "create" or operation.project_id != project_id:
        raise ValidationError("The saved upload does not match this deliverable")
    for name in names:
        expected = digest(
            {
                "project_id": str(project_id),
                "kind": "create",
                "data": {"name": name, "parent": parent, "mime": media_type, "sha256": sha256},
            }
        )
        if operation.request_digest == expected:
            return name
    raise ValidationError("The saved upload belongs to a different file or destination")
