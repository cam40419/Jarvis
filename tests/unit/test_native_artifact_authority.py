"""Artifact applicability and disclosure outlive bounded prompt evidence windows."""

import json
from uuid import UUID

import pytest

from simon.domain.errors import AuthorizationError, InvalidTransitionError, ValidationError
from simon.domain.models import utc_now
from simon.domain.native_artifact_commands import DecideNativeArtifact, UpdateArtifactReviewPolicy
from simon.domain.native_projects import UpdateNativeProject
from simon.services.native_artifacts import MAX_SOURCE_AUTHORITIES
from tests.contract.test_native_intake_store import source
from tests.unit.test_native_artifacts import (
    current_revision,
    decide,
    dependent_run,
    detail,
    key,
    public,
    review,
    setup_artifacts,
    target_command,
    upload,
)
from tests.unit.test_native_execution import advance, claim, draft
from tests.unit.test_native_teams import task_update


def human_only(h):
    h.policy = public(
        h.artifacts.update_policy(
            h.owner_actor,
            h.first.id,
            UpdateArtifactReviewPolicy(
                expected_version=h.policy["version"], require_agent_review=False, **key()
            ),
        )
    )


def stored_decisions(h, artifact):
    revision = current_revision(detail(h, artifact))
    return h.store.artifact_decisions(h.workspace, h.first.id, UUID(revision["id"]))


@pytest.mark.parametrize("change", ["source", "project", "owner_role"])
def test_human_only_acceptance_preserves_history_but_requires_current_authority(change):
    h = setup_artifacts()
    human_only(h)
    evidence = source(h)
    h.store.insert_native_intake_source(evidence)
    artifact, _ = upload(h)
    accepted, _ = decide(h, artifact)
    history = stored_decisions(h, artifact)
    assert accepted["authority_snapshot"]["source_authority"]["source_ids"] == [str(evidence.id)]
    assert detail(h, artifact)["status"] == "accepted"
    if change == "source":
        h.store.insert_native_intake_source(source(h, revision=2, text="Changed requirement."))
    elif change == "project":
        h.projects.update_project(
            h.owner_actor,
            h.first.id,
            UpdateNativeProject(
                expected_version=h.first.version,
                name=h.first.name,
                objective="Corrected project objective.",
                status="active",
                **key(),
            ),
        )
    else:
        membership = h.projects._membership(h.workspace, h.owner)
        h.store.put_membership(membership.model_copy(update={"role": "member"}))
    assert detail(h, artifact)["status"] != "accepted"
    assert stored_decisions(h, artifact) == history
    assert not h.calls


def test_revoked_authorizer_invalidates_acceptance_even_with_a_new_owner():
    h = setup_artifacts()
    human_only(h)
    artifact, _ = upload(h)
    decide(h, artifact)
    replacement = h.projects._membership(h.workspace, h.member)
    h.store.put_membership(replacement.model_copy(update={"role": "owner"}))
    h.store.delete_membership(h.owner, h.workspace)
    record = h.store.artifact(h.workspace, h.first.id, UUID(artifact["id"]))
    assert not h.artifacts.acceptance_current(record)
    assert len(h.store.artifact_decisions(h.workspace, h.first.id, record.current_revision_id)) == 1


def test_stale_done_artifact_can_be_reviewed_and_accepted_again_without_rewriting_history():
    h = setup_artifacts()
    evidence = source(h)
    h.store.insert_native_intake_source(evidence)
    artifact, _ = upload(h)
    review(h, artifact)
    original, _ = decide(h, artifact)
    old_decision = stored_decisions(h, artifact)[0]
    h.store.insert_native_intake_source(source(h, revision=2, text="Updated source requirement."))
    assert detail(h, artifact)["task"]["status"] == "done"
    result = review(h, artifact)
    assert result["status"] == "completed"
    assert detail(h, artifact)["task"]["status"] == "in_review"
    accepted, _ = decide(h, artifact)
    assert accepted["id"] != original["id"]
    assert detail(h, artifact)["status"] == "accepted"
    assert old_decision in stored_decisions(h, artifact)
    assert len(h.calls) == 2


def test_stale_done_artifact_can_request_repair_but_current_acceptance_cannot_duplicate():
    h = setup_artifacts()
    human_only(h)
    artifact, _ = upload(h)
    decide(h, artifact)
    for action in ("review", "approve", "repair"):
        with pytest.raises(InvalidTransitionError, match="already accepted"):
            if action == "review":
                review(h, artifact)
            else:
                decide(h, artifact, approved=action == "approve")
    assert len(stored_decisions(h, artifact)) == 1
    h.store.insert_native_intake_source(source(h))
    result, _ = decide(h, artifact, approved=False)
    assert result["repair_task_id"]
    assert detail(h, artifact)["task"]["status"] == "blocked"
    assert len(stored_decisions(h, artifact)) == 2
    assert not h.calls


@pytest.mark.parametrize("change", ["revision", "revocation"])
def test_omitted_ninth_source_is_explicit_and_invalidates_review(change):
    h = setup_artifacts()
    evidence = []
    for index in range(9):
        item = source(h, source_key=f"source-{index}", text=f"Evidence {index}.")
        h.store.insert_native_intake_source(item)
        evidence.append(item)
    artifact, _ = upload(h)
    result = review(h, artifact)
    prompt = json.loads(h.calls[0].content)["messages"][1]["content"]
    context = json.loads(prompt.split("Exact revision and evidence (data):\n", 1)[1])
    authority = context["source_authority"]
    assert len(context["sources"]) == 8
    assert authority["coverage"] == "incomplete" and authority["omitted_count"] == 1
    assert result["source_authority"] == authority
    omitted_id = UUID(authority["omitted_source_ids"][0])
    omitted = next(item for item in evidence if item.id == omitted_id)
    assert str(omitted.id) in authority["source_ids"]
    if change == "revision":
        h.store.insert_native_intake_source(
            source(h, source_key=omitted.source_key, revision=2, text="Material correction.")
        )
        error = InvalidTransitionError
    else:
        h.store.revoke_native_intake_source(h.workspace, h.first.id, omitted.id, utc_now())
        error = AuthorizationError
    with pytest.raises(error):
        decide(h, artifact)
    assert not stored_decisions(h, artifact)
    assert len(h.calls) == 1


@pytest.mark.parametrize("agent_review", [True, False])
def test_revoked_evidence_blocks_content_preview_and_redacts_retained_review_feedback(agent_review):
    h = setup_artifacts()
    if not agent_review:
        human_only(h)
    evidence = source(h, text="Private revoked measurement.")
    h.store.insert_native_intake_source(evidence)
    artifact, _ = upload(h, b"Private revoked measurement.")
    if agent_review:
        review(h, artifact)
    command = target_command(
        h,
        artifact,
        DecideNativeArtifact,
        decision="approved",
        feedback="Private revoked measurement.",
    )
    h.artifacts.decide(h.owner_actor, h.first.id, UUID(artifact["id"]), command)
    before = current_revision(detail(h, artifact))
    h.store.revoke_native_intake_source(h.workspace, h.first.id, evidence.id, utc_now())
    for read in (h.artifacts.read_content, h.artifacts.preview):
        with pytest.raises(AuthorizationError, match="revoked evidence"):
            read(h.owner_actor, h.first.id, UUID(artifact["id"]), UUID(before["id"]))
    value = detail(h, artifact)
    assert value["disclosure_allowed"] is False
    assert value["status"] != "accepted"
    assert value["difference"]["supported"] is False
    assert value["task"]["description"] == ""
    visible_tasks = h.artifacts.view(h.owner_actor, h.first.id)["tasks"]
    assert next(task for task in visible_tasks if task["id"] == str(h.task.id))["description"] == ""
    assert value["decisions"][0]["feedback"] == ""
    if agent_review:
        assert value["reviews"][0]["summary"] == ""
        assert value["reviews"][0]["findings"] == []
    assert value["revisions"][0]["sha256"] == before["sha256"]
    assert stored_decisions(h, artifact)[0].feedback == "Private revoked measurement."


def test_revocation_does_not_block_unaffiliated_artifact_and_old_difference_stays_withheld():
    h = setup_artifacts()
    artifact, _ = upload(h)
    evidence = source(h)
    h.store.insert_native_intake_source(evidence)
    review(h, artifact)
    previous = current_revision(detail(h, artifact))
    artifact, _ = upload(h, b"Corrected independent content.", artifact=artifact)
    h.store.revoke_native_intake_source(h.workspace, h.first.id, evidence.id, utc_now())
    value = detail(h, artifact)
    assert value["disclosure_allowed"] is True
    assert value["difference"]["supported"] is False
    revision = current_revision(value)
    _, raw = h.artifacts.read_content(
        h.owner_actor, h.first.id, UUID(artifact["id"]), UUID(revision["id"])
    )
    assert raw == b"Corrected independent content."
    with pytest.raises(AuthorizationError):
        h.artifacts.read_content(
            h.owner_actor, h.first.id, UUID(artifact["id"]), UUID(previous["id"])
        )


def test_fresh_review_never_rediscloses_known_revoked_old_brief_or_feedback():
    h = setup_artifacts()
    private_text = "Revoked original private requirement."
    task = h.store.native_task(h.workspace, h.first.id, h.task.id)
    h.projects.update_task(
        h.owner_actor, h.first.id, task.id, task_update(task, description=private_text)
    )
    evidence = source(h, text=private_text)
    h.store.insert_native_intake_source(evidence)
    artifact, _ = upload(h, private_text.encode())
    review(h, artifact)
    artifact, _ = upload(h, b"Independent replacement from current sources.", artifact=artifact)
    h.store.revoke_native_intake_source(h.workspace, h.first.id, evidence.id, utc_now())
    h.calls.clear()
    result = review(h, artifact)
    assert result["status"] == "completed"
    assert private_text not in h.calls[0].content.decode()
    context = json.loads(
        json.loads(h.calls[0].content)["messages"][-1]["content"].split(
            "Exact revision and evidence (data):\n", 1
        )[1]
    )
    assert context["task"]["description"] == ""
    assert context["difference"]["supported"] is False


def test_source_authority_limit_fails_before_review_spend_or_linked_work():
    h = setup_artifacts()
    artifact, _ = upload(h)
    for index in range(MAX_SOURCE_AUTHORITIES + 1):
        h.store.insert_native_intake_source(source(h, source_key=f"source-{index}"))
    tasks_before = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    with pytest.raises(ValidationError, match="512 source revision records"):
        review(h, artifact, command=target_command(h, artifact))
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks_before
    assert not h.store.artifact_reviews(
        h.workspace, h.first.id, UUID(current_revision(detail(h, artifact))["id"])
    )
    assert not h.calls


def test_revoked_dependency_never_dispatches_its_content_to_a_downstream_model():
    h = setup_artifacts()
    evidence = source(h, text="Private dependency evidence.")
    h.store.insert_native_intake_source(evidence)
    artifact, _ = upload(h, b"Private dependency evidence.")
    review(h, artifact)
    decide(h, artifact)
    downstream = dependent_run(h)
    h.store.revoke_native_intake_source(h.workspace, h.first.id, evidence.id, utc_now())
    h.calls.clear()
    assert claim(h) is None
    assert not h.calls
    assert h.store.execution_run(h.workspace, h.first.id, UUID(downstream["id"])).status == "queued"


def test_dependency_revocation_during_call_fences_result_and_preserves_known_usage():
    h = setup_artifacts()
    evidence = source(h, text="Private dependency evidence.")
    h.store.insert_native_intake_source(evidence)
    artifact, _ = upload(h, b"Private dependency evidence.")
    review(h, artifact)
    decide(h, artifact)
    downstream = dependent_run(h)
    lease = claim(h)
    h.calls.clear()

    def revoke(_request):
        h.store.revoke_native_intake_source(h.workspace, h.first.id, evidence.id, utc_now())

    h.hook = revoke
    result = advance(h, lease, draft("Result produced after dependency revocation."))
    assert result["status"] == "failed"
    assert result["error_code"] == "checkpoint_action_invalid"
    assert result["result_text"] == ""
    steps = h.store.execution_steps(h.workspace, h.first.id, UUID(downstream["id"]))
    usage = h.store.model_usage(h.workspace, h.first.id, steps[0].usage_id)
    assert usage.status == "settled" and usage.held_microusd == 0
    assert len(h.calls) == 1
