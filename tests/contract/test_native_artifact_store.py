"""Artifacts preserve bytes and scoped ancestry independently of acceptance authority."""

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_artifacts import (
    NativeArtifact,
    NativeArtifactCheck,
    NativeArtifactDecision,
    NativeArtifactReview,
    NativeArtifactRevision,
    NativeReviewPolicy,
)
from simon.domain.native_projects import TaskAssignment
from tests.contract.test_native_execution_store import run_record, seed_execution


@pytest.fixture
def artifacts(store):
    h = seed_execution(store)
    h.reviewer = h.agent.model_copy(
        update={"id": uuid4(), "role_key": "reviewer", "name": "Reviewer"}
    )
    store.insert_native_agent(h.reviewer)
    h.artifact = NativeArtifact(
        workspace_id=h.workspace,
        project_id=h.first.id,
        task_id=h.task.id,
        name="Deliverable",
        created_by_agent_id=h.agent.id,
    )
    store.insert_artifact(h.artifact)
    h.revision = revision_record(h)
    store.insert_artifact_revision(h.revision)
    h.artifact = h.artifact.model_copy(update={"version": 2, "current_revision_id": h.revision.id})
    store.update_artifact(h.artifact, 1)
    return h


def revision_record(h, **changes):
    content = b"Original immutable artifact\n"
    return NativeArtifactRevision(
        **{
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "artifact_id": h.artifact.id,
            "task_id": h.task.id,
            "sequence": 1,
            "filename": "result.txt",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content).decode("ascii"),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "created_by_agent_id": h.agent.id,
            **changes,
        }
    )


def review_record(h, **changes):
    return NativeArtifactReview(
        **{
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "artifact_id": h.artifact.id,
            "revision_id": h.revision.id,
            "sha256": h.revision.sha256,
            "artifact_version": h.artifact.version,
            "policy_version": 0,
            "task_version": h.task.version,
            "project_version": h.first.version,
            "reviewer_agent_id": h.reviewer.id,
            "reviewer_version": h.reviewer.version,
            "model_id": h.model.id,
            "model_version": h.model.version,
            "endpoint_fingerprint": "e" * 64,
            "requested_by": h.owner,
            "idempotency_key": "artifact-review-key",
            "request_digest": "b" * 64,
            "context": {"candidate": "Immutable reviewer input"},
            **changes,
        }
    )


def test_artifact_bytes_scope_and_one_deliverable_per_task(artifacts):
    h = artifacts
    assert h.store.artifact(h.workspace, h.first.id, h.artifact.id) == h.artifact
    assert h.store.artifact_revision(h.workspace, h.first.id, h.revision.id) == h.revision
    assert h.store.artifacts(h.workspace, h.first.id, h.task.id) == (h.artifact,)
    assert h.store.artifact_revisions(h.workspace, h.first.id, h.artifact.id) == (h.revision,)
    assert h.store.artifacts(h.workspace, h.first.id, offset=1) == ()
    assert h.store.artifact_revisions(h.workspace, h.first.id, h.artifact.id, offset=1) == ()
    for ws, pid in ((h.workspace, h.second.id), (h.foreign_workspace, h.first.id)):
        assert h.store.artifact(ws, pid, h.artifact.id) is None
        assert h.store.artifact_revision(ws, pid, h.revision.id) is None
        assert h.store.artifacts(ws, pid) == ()
        assert h.store.artifact_revisions(ws, pid, h.artifact.id) == ()
    assert base64.b64decode(h.revision.content_base64) == b"Original immutable artifact\n"
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact(
            h.artifact.model_copy(update={"id": uuid4(), "version": 1, "current_revision_id": None})
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": uuid4()},
        {"task_id": uuid4()},
        {"created_by_agent_id": uuid4()},
        {"created_by_agent_id": None, "created_by": uuid4()},
        {"version": 4},
        {"current_revision_id": uuid4()},
        {"pending_repair_task_id": uuid4()},
    ],
)
def test_artifact_rejects_invalid_scope_and_initial_authority(artifacts, changes):
    h = artifacts
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact(
            h.artifact.model_copy(
                update={
                    "id": uuid4(),
                    "task_id": h.other.id,
                    "version": 1,
                    "current_revision_id": None,
                    **changes,
                }
            )
        )


def test_artifact_head_cas_ancestry_and_repair_task_mapping(artifacts):
    h = artifacts
    for changes in (
        {"task_id": h.other.id},
        {"current_revision_id": None},
        {"version": 5},
        {"pending_repair_task_id": h.task.id},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.update_artifact(h.artifact.model_copy(update={"version": 3, **changes}), 2)
    pending = h.artifact.model_copy(update={"version": 3, "pending_repair_task_id": h.other.id})
    h.store.update_artifact(pending, 2)
    assert h.store.artifacts(h.workspace, h.first.id, h.other.id) == (pending,)
    run = run_record(h, task_id=h.other.id)
    h.store.insert_execution_run(run)
    revision = revision_record(
        h, task_id=h.other.id, sequence=2, parent_revision_id=h.revision.id, run_id=run.id
    )
    h.store.insert_artifact_revision(revision)
    updated = pending.model_copy(
        update={"version": 4, "current_revision_id": revision.id, "pending_repair_task_id": None}
    )
    h.store.update_artifact(updated, 3)
    assert h.store.artifacts(h.workspace, h.first.id, h.other.id) == ()
    assert h.store.artifact_revisions(h.workspace, h.first.id, h.artifact.id) == (
        h.revision,
        revision,
    )
    with pytest.raises(InvalidTransitionError):
        h.store.update_artifact(
            updated.model_copy(update={"version": 5, "current_revision_id": h.revision.id}), 4
        )
    with pytest.raises(InvalidTransitionError):
        h.store.update_artifact(updated, 3)


def test_original_and_repair_tasks_map_to_one_deliverable(artifacts):
    h = artifacts
    other = h.artifact.model_copy(
        update={
            "id": uuid4(),
            "task_id": h.other.id,
            "version": 1,
            "current_revision_id": None,
        }
    )
    h.store.insert_artifact(other)
    with pytest.raises(InvalidTransitionError):
        h.store.update_artifact(
            h.artifact.model_copy(update={"version": 3, "pending_repair_task_id": h.other.id}), 2
        )
    repair = h.task.model_copy(update={"id": uuid4(), "title": "Repair deliverable"})
    h.store.insert_native_task(repair)
    pending = h.artifact.model_copy(update={"version": 3, "pending_repair_task_id": repair.id})
    h.store.update_artifact(pending, 2)
    with pytest.raises(InvalidTransitionError):
        h.store.update_artifact(
            other.model_copy(update={"version": 2, "pending_repair_task_id": repair.id}), 1
        )
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact(other.model_copy(update={"id": uuid4(), "task_id": repair.id}))


def test_review_independence_keeps_original_author_and_current_task_maker(artifacts):
    h = artifacts
    revision = revision_record(
        h,
        sequence=2,
        parent_revision_id=h.revision.id,
        created_by_agent_id=None,
        created_by=h.owner,
    )
    h.store.insert_artifact_revision(revision)
    h.artifact = h.artifact.model_copy(update={"version": 3, "current_revision_id": revision.id})
    h.store.update_artifact(h.artifact, 2)
    h.revision = revision
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_review(review_record(h, reviewer_agent_id=h.agent.id))
    h.task = h.task.model_copy(
        update={"version": 2, "assignment": TaskAssignment(kind="agent", agent_id=h.reviewer.id)}
    )
    h.store.update_native_task(h.task, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_review(review_record(h))
    h.task = h.task.model_copy(update={"version": 3, "assignment": TaskAssignment()})
    h.store.update_native_task(h.task, 2)
    review = review_record(h)
    h.store.insert_artifact_review(review)
    h.task = h.task.model_copy(
        update={"version": 4, "assignment": TaskAssignment(kind="agent", agent_id=h.reviewer.id)}
    )
    h.store.update_native_task(h.task, 3)
    stale = review.model_copy(update={"status": "stale", "version": 2, "finished_at": utc_now()})
    h.store.update_artifact_review(stale, 1)
    assert h.store.artifact_review(h.workspace, h.first.id, review.id) == stale


def test_review_work_item_is_separate_scoped_assigned_and_versioned(artifacts):
    h = artifacts
    work = h.other.model_copy(
        update={
            "version": 2,
            "assignment": TaskAssignment(kind="agent", agent_id=h.reviewer.id),
        }
    )
    h.store.update_native_task(work, 1)
    review = review_record(h, review_task_id=work.id, review_task_version=work.version)
    for changes in (
        {"review_task_id": uuid4()},
        {"review_task_id": h.task.id},
        {"review_task_version": None},
        {"review_task_version": 1},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_artifact_review(review.model_copy(update=changes))
    h.store.insert_artifact_review(review)
    with pytest.raises(InvalidTransitionError):
        h.store.update_artifact_review(
            review.model_copy(update={"version": 2, "review_task_version": 3}), 1
        )
    h.store.update_native_task(
        work.model_copy(update={"version": 3, "title": "Criteria changed"}), 2
    )
    stale = review.model_copy(update={"status": "stale", "version": 2, "finished_at": utc_now()})
    h.store.update_artifact_review(stale, 1)
    assert h.store.artifact_review(h.workspace, h.first.id, review.id) == stale


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": "0" * 64},
        {"size_bytes": 0},
        {"content_base64": "invalid!"},
        {"artifact_id": uuid4()},
        {"task_id": uuid4()},
        {"run_id": uuid4()},
        {"parent_revision_id": uuid4()},
        {"sequence": 3},
        {"filename": "../escaped"},
    ],
)
def test_revision_revalidates_exact_bytes_and_ancestry_at_storage_boundary(artifacts, changes):
    h = artifacts
    candidate = h.revision.model_copy(
        update={"id": uuid4(), "sequence": 2, "parent_revision_id": h.revision.id, **changes}
    )
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_revision(candidate)
    assert h.store.artifact_revisions(h.workspace, h.first.id, h.artifact.id) == (h.revision,)


def test_revision_identifier_cannot_be_reused_in_another_project(artifacts):
    h = artifacts
    task = h.task.model_copy(update={"id": uuid4(), "project_id": h.second.id})
    h.store.insert_native_task(task)
    artifact = h.artifact.model_copy(
        update={
            "id": uuid4(),
            "project_id": h.second.id,
            "task_id": task.id,
            "current_revision_id": None,
            "version": 1,
            "created_by_agent_id": None,
            "created_by": h.owner,
        }
    )
    h.store.insert_artifact(artifact)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_revision(
            h.revision.model_copy(
                update={
                    "project_id": h.second.id,
                    "artifact_id": artifact.id,
                    "task_id": task.id,
                    "created_by_agent_id": None,
                    "created_by": h.owner,
                }
            )
        )
    assert h.store.artifact_revisions(h.workspace, h.second.id, artifact.id) == ()


def test_concurrent_revision_sequences_have_one_winner(artifacts):
    h = artifacts
    candidates = [
        revision_record(h, sequence=2, parent_revision_id=h.revision.id) for _ in range(2)
    ]

    def publish(candidate):
        try:
            h.store.insert_artifact_revision(candidate)
            return candidate
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = tuple(value for value in executor.map(publish, candidates) if value)
    assert len(winners) == 1
    assert h.store.artifact_revisions(h.workspace, h.first.id, h.artifact.id) == (
        h.revision,
        winners[0],
    )


def test_review_policy_versions_scope_and_authority(artifacts):
    h = artifacts
    policy = NativeReviewPolicy(
        workspace_id=h.workspace, project_id=h.first.id, reviewer_agent_id=h.reviewer.id, version=1
    )
    h.store.save_review_policy(policy, 0)
    assert h.store.review_policy(h.workspace, h.first.id) == policy
    assert h.store.review_policy(h.workspace, h.second.id) is None
    h.store.save_review_policy(
        policy.model_copy(update={"version": 2, "require_agent_review": False}), 1
    )
    for changes in (
        {"version": 5},
        {"require_agent_review": False, "require_human_approval": False},
        {"reviewer_agent_id": uuid4()},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.save_review_policy(policy.model_copy(update={"version": 3, **changes}), 2)
    with pytest.raises(InvalidTransitionError):
        h.store.save_review_policy(policy, 0)


def test_deterministic_checks_are_exact_immutable_and_detached(artifacts):
    h = artifacts
    check = NativeArtifactCheck(
        workspace_id=h.workspace,
        project_id=h.first.id,
        revision_id=h.revision.id,
        sha256=h.revision.sha256,
        check_name="content_integrity",
        status="passed",
        details={"size": h.revision.size_bytes},
    )
    h.store.insert_artifact_check(check)
    assert h.store.artifact_checks(h.workspace, h.second.id, h.revision.id) == ()
    detached = h.store.artifact_checks(h.workspace, h.first.id, h.revision.id)[0]
    detached.details["size"] = 0
    assert h.store.artifact_checks(h.workspace, h.first.id, h.revision.id) == (check,)
    for changes in (
        {},
        {"id": uuid4()},
        {"id": uuid4(), "sha256": "0" * 64},
        {"id": uuid4(), "project_id": h.second.id},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_artifact_check(check.model_copy(update=changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"reviewer_agent_id": None},
        {"reviewer_agent_id": uuid4()},
        {"model_id": uuid4()},
        {"requested_by": uuid4()},
        {"revision_id": uuid4()},
        {"sha256": "0" * 64},
        {"artifact_id": uuid4()},
        {"usage_id": uuid4()},
        {"artifact_version": 1},
        {"policy_version": 2},
        {"task_version": 3},
        {"project_version": 3},
        {"reviewer_version": 3},
        {"model_version": 3},
    ],
)
def test_model_reviews_reject_foreign_or_stale_authority(artifacts, changes):
    h = artifacts
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_review(review_record(h).model_copy(update=changes))


def test_model_review_cas_immutable_inputs_results_and_independence(artifacts):
    h = artifacts
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_review(review_record(h, reviewer_agent_id=h.agent.id))
    review = review_record(h)
    h.store.insert_artifact_review(review)
    assert h.store.artifact_review(h.workspace, h.second.id, review.id) is None
    assert h.store.artifact_reviews(h.workspace, h.first.id, h.revision.id) == (review,)
    assert h.store.artifact_reviews(h.workspace, h.first.id, h.revision.id, offset=1) == ()
    detached = h.store.artifact_review(h.workspace, h.first.id, review.id)
    detached.context["candidate"] = "Changed copy"
    assert h.store.artifact_review(h.workspace, h.first.id, review.id) == review
    for changes in (
        {"context": {}},
        {"revision_id": uuid4()},
        {"endpoint_fingerprint": "a" * 64},
        {"version": 3},
        {
            "status": "completed",
            "verdict": "approved",
            "findings": ("Unresolved issue",),
            "finished_at": utc_now(),
        },
        {"status": "completed", "verdict": "changes_requested", "finished_at": utc_now()},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.update_artifact_review(review.model_copy(update={"version": 2, **changes}), 1)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_review(review_record(h, idempotency_key="duplicate-review"))
    completed = review.model_copy(
        update={
            "version": 2,
            "status": "completed",
            "verdict": "approved",
            "summary": "Candidate meets criteria",
            "finished_at": utc_now(),
        }
    )
    h.store.update_artifact_review(completed, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.update_artifact_review(
            completed.model_copy(update={"version": 3, "summary": "Rewritten"}), 2
        )
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_review(review_record(h))
    second = review_record(
        h, idempotency_key="second-review-key", created_at=review.created_at + timedelta(seconds=1)
    )
    h.store.insert_artifact_review(second)
    unknown = second.model_copy(
        update={
            "status": "unknown",
            "version": 2,
            "finished_at": second.created_at + timedelta(seconds=1),
        }
    )
    h.store.update_artifact_review(unknown, 1)
    cancelled = unknown.model_copy(update={"status": "cancelled", "version": 3})
    h.store.update_artifact_review(cancelled, 2)
    assert h.store.artifact_reviews(h.workspace, h.first.id, h.revision.id) == (
        completed,
        cancelled,
    )


def test_decisions_bind_content_reviews_and_repair_tasks(artifacts):
    h = artifacts
    review = review_record(h)
    h.store.insert_artifact_review(review)
    decision = NativeArtifactDecision(
        workspace_id=h.workspace,
        project_id=h.first.id,
        artifact_id=h.artifact.id,
        revision_id=h.revision.id,
        sha256=h.revision.sha256,
        policy_version=0,
        task_version=h.task.version,
        decided_by=h.owner,
        review_id=review.id,
        decision="changes_requested",
        feedback="Resolve the missing criteria",
        repair_task_id=h.other.id,
    )
    h.store.insert_artifact_decision(decision)
    assert h.store.artifact_decisions(h.workspace, h.first.id, h.revision.id) == (decision,)
    assert h.store.artifact_decisions(h.workspace, h.second.id, h.revision.id) == ()
    for changes in (
        {},
        {"id": uuid4(), "sha256": "0" * 64},
        {"id": uuid4(), "review_id": uuid4()},
        {"id": uuid4(), "repair_task_id": uuid4()},
        {"id": uuid4(), "decided_by": h.outsider},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_artifact_decision(decision.model_copy(update=changes))


def test_admitted_review_can_settle_after_requester_membership_revocation(artifacts):
    h = artifacts
    review = review_record(h)
    h.store.insert_artifact_review(review)
    h.store.delete_membership(h.owner, h.workspace)
    stale = review.model_copy(
        update={
            "status": "stale",
            "version": 2,
            "summary": "Requester authority revoked",
            "finished_at": utc_now(),
        }
    )
    h.store.update_artifact_review(stale, 1)
    assert h.store.artifact_review(h.workspace, h.first.id, review.id) == stale
    with pytest.raises(InvalidTransitionError):
        h.store.insert_artifact_review(review_record(h, idempotency_key="new-after-revocation"))


def test_artifact_publication_and_review_transaction_roll_back(artifacts):
    h = artifacts
    revision = revision_record(h, sequence=2, parent_revision_id=h.revision.id)
    updated = h.artifact.model_copy(update={"version": 3, "current_revision_id": revision.id})
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.insert_artifact_revision(revision)
        h.store.update_artifact(updated, 2)
        raise RuntimeError("Injected publication failure")
    assert h.store.artifact(h.workspace, h.first.id, h.artifact.id) == h.artifact
    assert h.store.artifact_revision(h.workspace, h.first.id, revision.id) is None
    review = review_record(h)
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.insert_artifact_review(review)
        raise RuntimeError("Injected review failure")
    assert h.store.artifact_review(h.workspace, h.first.id, review.id) is None


@pytest.mark.postgres
def test_postgres_direct_history_rewrite_is_rejected(postgres_url):
    import psycopg

    from simon.adapters.postgres import PostgresStore
    from simon.seed import seed_development_identity

    seed_development_identity(postgres_url)
    store = PostgresStore(postgres_url)
    h = seed_execution(store)
    h.artifact = NativeArtifact(
        workspace_id=h.workspace,
        project_id=h.first.id,
        task_id=h.task.id,
        name="Atomic",
        created_by=h.owner,
    )
    store.insert_artifact(h.artifact)
    h.revision = revision_record(h)
    store.insert_artifact_revision(h.revision)
    h.artifact = h.artifact.model_copy(update={"version": 2, "current_revision_id": h.revision.id})
    store.update_artifact(h.artifact, 1)
    h.reviewer = h.agent.model_copy(
        update={"id": uuid4(), "role_key": "reviewer", "name": "Reviewer"}
    )
    store.insert_native_agent(h.reviewer)
    review = review_record(h)
    store.insert_artifact_review(review)
    for changes in ({"context": {}}, {"sha256": "a" * 64}):
        with pytest.raises(InvalidTransitionError):
            store._artifact_write(
                "native_artifact_reviews", review.model_copy(update={"version": 2, **changes}), 1
            )
    completed = review.model_copy(
        update={
            "version": 2,
            "status": "completed",
            "verdict": "approved",
            "finished_at": utc_now(),
        }
    )
    store.update_artifact_review(completed, 1)
    for changes in ({"summary": "Changed outcome"}, {"context": {}}):
        with pytest.raises(InvalidTransitionError):
            store._artifact_write(
                "native_artifact_reviews", completed.model_copy(update={"version": 3, **changes}), 2
            )
    for changes in ({"sha256": "a" * 64}, {"size_bytes": 0}, {"content_base64": ""}):
        with pytest.raises(InvalidTransitionError):
            store._artifact_write(
                "native_artifact_revisions",
                h.revision.model_copy(
                    update={
                        "id": uuid4(),
                        "sequence": 2,
                        "parent_revision_id": h.revision.id,
                        **changes,
                    }
                ),
                None,
            )
    check = NativeArtifactCheck(
        workspace_id=h.workspace,
        project_id=h.first.id,
        revision_id=h.revision.id,
        sha256=h.revision.sha256,
        check_name="content_integrity",
        status="passed",
    )
    store.insert_artifact_check(check)
    for table, identifier in (
        ("native_artifact_revisions", h.revision.id),
        ("native_artifact_checks", check.id),
    ):
        for operation in (
            f"UPDATE {table} SET snapshot=snapshot WHERE id=%s",
            f"DELETE FROM {table} WHERE id=%s",
        ):
            with pytest.raises(psycopg.IntegrityError), store.transaction():
                store.connection.execute(operation, (identifier,))
    store.close()
