from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from simon.domain.artifact_reviews import AcceptArtifactReview, FileCheck, RecordArtifactReview
from simon.domain.errors import IdempotencyConflictError, InvalidTransitionError, NotFoundError
from simon.services.artifact_reviews import ArtifactReviewService
from tests.unit.test_agent_dispatcher import ControlledModel, make_harness, task


@pytest.fixture
def reviewed_run(tmp_path):
    harness = make_harness(tmp_path)
    queued = harness.queue((task("draft"), task("alternative")))
    dispatcher = harness.dispatcher(ControlledModel())
    run = dispatcher.execute(queued.id)
    return harness, run, dispatcher.artifacts


def request(artifact, **changes):
    return RecordArtifactReview(
        **{
            "idempotency_key": str(uuid4()),
            "artifact_id": artifact.id,
            "expected_sha256": artifact.sha256,
            "delivery_key": "report",
            "inspected": True,
            "verdict": "passed",
            "checks": (
                FileCheck(
                    format="text",
                    procedure="Read full report",
                    observed="All required sections present",
                    passed=True,
                ),
            ),
            **changes,
        }
    )


def test_review_retry_is_immutable_and_survives_service_recreation(reviewed_run):
    harness, run, _ = reviewed_run
    service = ArtifactReviewService(harness.runs)
    body = request(run.tasks[0].artifacts[0])
    review = service.record(harness.actor, run.id, body)
    assert service.record(harness.actor, run.id, body) == review
    assert ArtifactReviewService(harness.runs).list(harness.actor, run.id) == (review,)
    with pytest.raises(IdempotencyConflictError):
        service.record(
            harness.actor, run.id, body.model_copy(update={"verdict": "changes_requested"})
        )


def test_repair_acceptance_preserves_previous_revision_and_rejects_stale_promotion(reviewed_run):
    harness, run, store = reviewed_run
    service = ArtifactReviewService(harness.runs)
    first, second = [
        service.record(harness.actor, run.id, request(task.artifacts[0])) for task in run.tasks
    ]
    accepted = service.accept(harness.actor, first.id, AcceptArtifactReview(expected_version=0))
    assert accepted.version == 1
    assert (
        service.accept(harness.actor, first.id, AcceptArtifactReview(expected_version=0))
        == accepted
    )
    with pytest.raises(InvalidTransitionError, match="changed"):
        service.accept(harness.actor, second.id, AcceptArtifactReview(expected_version=0))
    replaced = service.accept(harness.actor, second.id, AcceptArtifactReview(expected_version=1))
    assert replaced.version == 2
    assert [item.review_id for item in replaced.revisions] == [first.id, second.id]
    assert all(store.read(item.artifact) for item in replaced.revisions)
    with pytest.raises(InvalidTransitionError):
        service.accept(harness.actor, first.id, AcceptArtifactReview(expected_version=0))
    assert service.acceptance(harness.actor, first.id) == replaced


def test_concurrent_acceptance_has_one_winner(reviewed_run):
    harness, run, _ = reviewed_run
    service = ArtifactReviewService(harness.runs)
    reviews = [
        service.record(harness.actor, run.id, request(task.artifacts[0])) for task in run.tasks
    ]

    def accept(review):
        try:
            service.accept(harness.actor, review.id, AcceptArtifactReview(expected_version=0))
            return True
        except InvalidTransitionError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(accept, reviews)) == [False, True]


def test_failed_review_cannot_replace_accepted_output(reviewed_run):
    harness, run, _ = reviewed_run
    service = ArtifactReviewService(harness.runs)
    review = service.record(
        harness.actor, run.id, request(run.tasks[0].artifacts[0], verdict="changes_requested")
    )
    with pytest.raises(InvalidTransitionError, match="passing"):
        service.accept(harness.actor, review.id, AcceptArtifactReview(expected_version=0))
    assert service.acceptance(harness.actor, review.id).version == 0


def test_newer_failed_evidence_blocks_older_passing_review(reviewed_run):
    harness, run, _ = reviewed_run
    service = ArtifactReviewService(harness.runs)
    artifact = run.tasks[0].artifacts[0]
    passing = service.record(harness.actor, run.id, request(artifact))
    service.record(harness.actor, run.id, request(artifact, verdict="changes_requested"))
    with pytest.raises(InvalidTransitionError, match="evidence changed"):
        service.accept(harness.actor, passing.id, AcceptArtifactReview(expected_version=0))


def test_stale_hash_and_foreign_artifact_are_rejected(reviewed_run):
    harness, run, _ = reviewed_run
    service = ArtifactReviewService(harness.runs)
    artifact = run.tasks[0].artifacts[0]
    with pytest.raises(InvalidTransitionError):
        service.record(harness.actor, run.id, request(artifact, expected_sha256="0" * 64))
    with pytest.raises(NotFoundError):
        service.record(harness.actor, run.id, request(artifact, artifact_id=uuid4()))


def test_foreign_actor_cannot_read_review_or_acceptance(reviewed_run):
    harness, run, _ = reviewed_run
    service = ArtifactReviewService(harness.runs)
    review = service.record(harness.actor, run.id, request(run.tasks[0].artifacts[0]))
    foreign = harness.actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(NotFoundError):
        service.get(foreign, review.id)
    with pytest.raises(NotFoundError):
        service.acceptance(foreign, review.id)


def test_altered_artifact_cannot_be_accepted_after_review(reviewed_run):
    from simon.domain.artifacts import ArtifactError

    harness, run, store = reviewed_run
    service = ArtifactReviewService(harness.runs)
    artifact = run.tasks[0].artifacts[0]
    review = service.record(harness.actor, run.id, request(artifact))
    (store._directory(artifact) / "content").write_bytes(b"changed after review")
    with pytest.raises(ArtifactError):
        service.accept(harness.actor, review.id, AcceptArtifactReview(expected_version=0))
    assert service.acceptance(harness.actor, review.id).version == 0
