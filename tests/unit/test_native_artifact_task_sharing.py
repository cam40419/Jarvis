"""Legacy task aliases cannot mutate an unrelated output's approval authority."""

from copy import deepcopy
from uuid import UUID

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.native_artifact_commands import DecideNativeArtifact
from tests.unit.test_native_artifacts import (
    current_revision,
    decide,
    detail,
    target_command,
    upload,
)
from tests.unit.test_native_delivery import setup_delivery
from tests.unit.test_native_execution import new_task


@pytest.mark.parametrize("alias", ["original", "maker", "repair"])
def test_legacy_shared_task_rejects_publication_and_decision_without_mutating_history(alias):
    h = setup_delivery()
    original, _ = upload(h, b"First exact approved output.")
    other, _ = upload(h, b"Unrelated exact approved output.", task=new_task(h))
    decide(h, original)
    decide(h, other)
    shared_task_id = UUID(original["task_id"])
    other_id = UUID(other["id"])
    other_revision_id = UUID(current_revision(detail(h, other))["id"])

    # Simulate a previously persisted/imported anomaly. Current insertion guards
    # prevent creating these aliases through the supported store/API contract.
    if alias == "maker":
        table = h.store._artifact_records["native_artifact_revisions"]
        table[other_revision_id] = table[other_revision_id].model_copy(
            update={"task_id": shared_task_id}
        )
    else:
        table = h.store._artifact_records["native_artifacts"]
        field = "task_id" if alias == "original" else "pending_repair_task_id"
        table[other_id] = table[other_id].model_copy(update={field: shared_task_id})

    artifacts_before = deepcopy(h.store._artifact_records)
    tasks_before = deepcopy(h.store._native_tasks)
    usage_before = h.store.storage_usage(h.workspace)
    statuses_before = (detail(h, original)["status"], detail(h, other)["status"])
    decision = target_command(
        h,
        original,
        DecideNativeArtifact,
        decision="changes_requested",
        feedback="Change the first output only.",
    )

    with pytest.raises(InvalidTransitionError, match="shares an original, maker or repair task"):
        upload(h, b"Replacement must not publish.", artifact=original)
    with pytest.raises(InvalidTransitionError, match="shares an original, maker or repair task"):
        h.artifacts.decide(h.owner_actor, h.first.id, UUID(original["id"]), decision)

    assert h.store._artifact_records == artifacts_before
    assert h.store._native_tasks == tasks_before
    assert h.store.storage_usage(h.workspace) == usage_before
    assert (detail(h, original)["status"], detail(h, other)["status"]) == statuses_before
    assert not h.calls
