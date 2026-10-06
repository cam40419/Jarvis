"""Verify saved predecessor outputs and stage bounded copies for a dependent task."""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import UUID

from simon.domain.agent_runs import TaskExecution
from simon.domain.artifacts import ArtifactError, DependencyArtifact
from simon.domain.models import ActorContext
from simon.services.artifacts import ArtifactStore
from simon.services.local_files import reject_links

MANIFEST_NAME = "dependency-inputs.json"


def _create_file(path: Path, content: bytes) -> None:
    reject_links(path)
    try:
        with path.open("xb") as target:
            target.write(content)
    except OSError:
        raise ArtifactError("Dependency input could not be created exclusively") from None


def prepare_dependency_artifacts(
    store: ArtifactStore,
    *,
    actor: ActorContext,
    run_id: UUID,
    dependencies: tuple[TaskExecution, ...],
    task_ids: Mapping[str, UUID],
    workspace: Path | None,
    revalidate: Callable[[], None],
) -> tuple[DependencyArtifact, ...]:
    """Accept only successful dependencies selected from the saved plan and run.

    The dispatcher publishes the text answer first, followed by native deliverables.
    ZIP bundles stay intact: importing never extracts or executes untrusted contents.
    Copies live at generated leaves of the owned mount, avoiding writable ancestors.
    A manifest is provenance of imported bytes, not evidence of a worker's review.
    """
    references: list[DependencyArtifact] = []
    total = 0
    for dependency in dependencies:
        if dependency.status != "succeeded" or dependency.id not in task_ids:
            raise ArtifactError("Artifact dependency is not an authorized completed task")
        for artifact in dependency.artifacts[1:]:
            if (
                artifact.workspace_id != actor.workspace_id
                or artifact.actor_id != actor.actor_id
                or artifact.run_id != run_id
                or artifact.task_id != task_ids[dependency.id]
            ):
                raise ArtifactError("Artifact is outside the dependency assignment")
            total += artifact.size
            if total > store.max_bytes or len(references) >= 32:
                raise ArtifactError("Dependency artifacts exceed the task input limit")
            references.append(
                DependencyArtifact(
                    dependency_id=dependency.id,
                    artifact=artifact,
                    workspace_path=(
                        f"dependency-{artifact.id}-{artifact.name}"
                        if workspace is not None
                        else None
                    ),
                )
            )
    # Verify every source before creating any destination. A metadata-only handoff
    # also verifies integrity, but does not claim to have made the bytes accessible.
    if not references:
        return ()
    revalidate()
    contents = [(reference, store.read(reference.artifact)) for reference in references]
    revalidate()
    if workspace is not None and references:
        reject_links(workspace)
        for reference, content in contents:
            assert reference.workspace_path is not None
            destination = workspace / reference.workspace_path
            revalidate()
            _create_file(destination, content)
        manifest = workspace / MANIFEST_NAME
        revalidate()
        _create_file(
            manifest,
            json.dumps(
                {"version": 1, "inputs": [item.model_dump(mode="json") for item in references]},
                indent=2,
            ).encode("utf-8"),
        )
    return tuple(references)
