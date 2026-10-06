import hashlib
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from simon.domain.agent_runs import TaskExecution
from simon.domain.artifacts import Artifact
from simon.domain.errors import ValidationError
from simon.domain.models import utc_now
from simon.domain.tasks import ProjectArtifact
from simon.services.output_classification import (
    deliverable_filename,
    is_legacy_response_artifact,
    is_response_artifact,
)


def artifact(name, text):
    return Artifact(
        id=uuid4(),
        workspace_id=uuid4(),
        actor_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        name=name,
        media_type="text/plain",
        size=len(text.encode()),
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        created_at=utc_now(),
    )


def test_controller_provenance_does_not_hide_a_named_export_or_unrelated_answer_file():
    answer = artifact("answer.txt", "Completed work")
    file = artifact("answer.txt", "Authored data file")
    task = TaskExecution(
        id="writer",
        agent_id="writer",
        output="Completed work",
        status="succeeded",
        artifacts=(answer, file),
    )
    assert is_response_artifact(task, answer)
    assert not is_response_artifact(task, file)
    assert not is_response_artifact(task.model_copy(update={"output": "Other content"}), answer)
    report = artifact("report.txt", "Completed work")
    assert not is_response_artifact(task.model_copy(update={"artifacts": (report,)}), report)


def test_legacy_summary_requires_controller_generated_identity():
    task_id = uuid4()
    identifier = uuid5(NAMESPACE_URL, f"simon:task-artifact:{task_id}:result.md")
    item = ProjectArtifact(
        id=identifier,
        workspace_id=uuid4(),
        actor_id=uuid4(),
        project_id=uuid4(),
        task_id=task_id,
        name="result.md",
        media_type="text/markdown",
        byte_count=5,
        sha256="0" * 64,
        created_at=utc_now(),
    )
    assert is_legacy_response_artifact(item)
    assert not is_legacy_response_artifact(item.model_copy(update={"id": uuid4()}))
    assert not is_legacy_response_artifact(item.model_copy(update={"name": "research.md"}))


@pytest.mark.parametrize(
    "path", ["research/Manufacturing economics.md", r"research\Manufacturing economics.md"]
)
def test_clean_filename_preserves_authored_basename(path):
    assert deliverable_filename(path) == "Manufacturing economics.md"


@pytest.mark.parametrize("path", ["", "..", "bad\nname.md", "bad\0name.md"])
def test_invalid_file_name_is_not_replaced_with_an_invented_name(path):
    with pytest.raises(ValidationError):
        deliverable_filename(path)
