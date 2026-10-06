from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from simon.domain.agent_platform import AgentProfile, AgentTaskSpec
from simon.domain.agent_runs import TaskExecution
from simon.domain.artifacts import Artifact, ArtifactError
from simon.domain.models import ActorContext, Channel
from simon.services.agent_prompts import render_agent_prompt
from simon.services.artifact_handoff import prepare_dependency_artifacts
from simon.services.artifacts import ArtifactStore


@dataclass
class Handoff:
    store: ArtifactStore
    actor: ActorContext
    run_id: UUID
    task_id: UUID
    artifact: Artifact
    workspace: Path

    def prepare(self, *, artifact=None, workspace=True, revalidate=lambda: None):
        return prepare_dependency_artifacts(
            self.store,
            actor=self.actor,
            run_id=self.run_id,
            dependencies=(
                TaskExecution(
                    id="maker",
                    agent_id="worker",
                    status="succeeded",
                    artifacts=(self.artifact, artifact or self.artifact),
                ),
            ),
            task_ids={"maker": self.task_id},
            workspace=self.workspace if workspace else None,
            revalidate=revalidate,
        )


@pytest.fixture
def handoff(tmp_path):
    actor = ActorContext(
        actor_id=uuid4(),
        workspace_id=uuid4(),
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    store = ArtifactStore(tmp_path / "artifacts", max_bytes=1024)
    run_id, task_id = uuid4(), uuid4()
    artifact = store.publish_bytes(
        workspace_id=actor.workspace_id,
        actor_id=actor.actor_id,
        run_id=run_id,
        task_id=task_id,
        content=b"part,dimension\nplate,12\n",
        name="dimensions.csv",
        media_type="text/csv",
    )
    workspace = tmp_path / "worker"
    workspace.mkdir()
    return Handoff(store, actor, run_id, task_id, artifact, workspace)


@pytest.mark.parametrize("field", ["workspace_id", "actor_id", "run_id", "task_id"])
def test_foreign_reference_fails_before_copy(handoff, field):
    with pytest.raises(ArtifactError, match="outside"):
        handoff.prepare(artifact=handoff.artifact.model_copy(update={field: uuid4()}))
    assert not list(handoff.workspace.iterdir())


def test_tampered_source_fails_before_copy(handoff):
    content = handoff.store._directory(handoff.artifact) / "content"
    content.write_bytes(b"changed")
    with pytest.raises(ArtifactError, match="integrity"):
        handoff.prepare()
    assert not list(handoff.workspace.iterdir())


def test_destination_collision_is_never_overwritten(handoff):
    inputs = handoff.prepare()
    destination = handoff.workspace / inputs[0].workspace_path
    destination.write_bytes(b"existing work")
    with pytest.raises(ArtifactError, match="exclusively"):
        handoff.prepare()
    assert destination.read_bytes() == b"existing work"


def test_revocation_after_read_prevents_copy(handoff):
    calls = 0

    def revalidate():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("Revoked")

    with pytest.raises(PermissionError, match="Revoked"):
        handoff.prepare(revalidate=revalidate)
    assert not list(handoff.workspace.iterdir())


def test_combined_input_limit_fails_before_copy(handoff):
    with pytest.raises(ArtifactError, match="limit"):
        handoff.prepare(artifact=handoff.artifact.model_copy(update={"size": 1025}))
    assert not list(handoff.workspace.iterdir())


def test_text_only_worker_gets_explicit_unavailable_reference(handoff):
    inputs = handoff.prepare(workspace=False)
    assert inputs[0].workspace_path is None
    assert not list(handoff.workspace.iterdir())
    rendered = render_agent_prompt(
        AgentProfile(
            id="reviewer", instructions="Summarize the results.", prompt_template="$objective"
        ),
        AgentTaskSpec(id="review", agent_id="reviewer", objective="Review", depends_on=("maker",)),
        {"maker": "Created a file"},
        dependency_artifacts=inputs,
    )
    assert '"workspace_path": null' in rendered.prompt
    assert handoff.artifact.sha256 in rendered.prompt
    assert "bytes are unavailable" in rendered.system


def test_archive_bytes_are_preserved_without_extraction(handoff):
    artifact = handoff.store.publish_bytes(
        workspace_id=handoff.actor.workspace_id,
        actor_id=handoff.actor.actor_id,
        run_id=handoff.run_id,
        task_id=handoff.task_id,
        content=b"opaque archive bytes",
        name="deliverables.zip",
        media_type="application/zip",
    )
    inputs = handoff.prepare(artifact=artifact)
    assert (handoff.workspace / inputs[0].workspace_path).read_bytes() == b"opaque archive bytes"
    assert len(list(handoff.workspace.iterdir())) == 2
