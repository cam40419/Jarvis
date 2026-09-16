from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from simon.domain.conversations import SubmitRun
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.identity import Membership
from simon.domain.interaction import SaveFeedback, SavePreferences
from simon.services.interaction import InteractionService
from tests.contract.test_model_runs import body
from tests.contract.test_model_runs import model_setup as model_setup


def preference_request(**changes):
    return SavePreferences(
        **{
            "profile": "auto",
            "answer_length": "auto",
            "auto_deep_enabled": False,
            "expected_version": 0,
            "idempotency_key": "preferences-001",
            **changes,
        }
    )


def feedback_request(**changes):
    return SaveFeedback(
        **{
            "rating": "helpful",
            "expected_version": 0,
            "idempotency_key": "feedback-001",
            **changes,
        }
    )


def test_preferences_are_personal_scoped_and_versioned(model_setup):
    service, actor, _model, _thread = model_setup
    interactions = InteractionService(service.store, service.audit)
    assert interactions.preferences(actor).version == 0
    request = preference_request()
    first = interactions.save_preferences(actor, request)
    assert first.version == 1 and first.auto_deep_enabled is False
    assert interactions.save_preferences(actor, request) == first
    with pytest.raises(IdempotencyConflictError):
        interactions.save_preferences(actor, request.model_copy(update={"profile": "quick"}))
    with pytest.raises(InvalidTransitionError):
        interactions.save_preferences(
            actor, preference_request(idempotency_key="preferences-stale")
        )
    newer = interactions.save_preferences(
        actor,
        preference_request(
            expected_version=1,
            profile="quick",
            idempotency_key="preferences-002",
        ),
    )
    assert interactions.save_preferences(actor, request) == newer
    other = actor.model_copy(update={"actor_id": uuid4()})
    assert interactions.preferences(other).version == 0
    other_home = actor.model_copy(update={"household_id": uuid4()})
    service.store.put_membership(
        Membership(actor_id=actor.actor_id, household_id=other_home.household_id, role="owner")
    )
    assert interactions.preferences(other_home).version == 0
    interactions.save_preferences(other_home, request)
    assert interactions.preferences(actor) == newer
    with pytest.raises(AuthorizationError):
        interactions.save_preferences(actor.model_copy(update={"scopes": frozenset()}), request)


def test_saved_deep_preference_changes_future_requests_only(model_setup):
    service, actor, model, thread = model_setup
    interactions = InteractionService(service.store, service.audit)
    question = SubmitRun(text="Debug this deadlock", idempotency_key="deep-question-001")
    original = service.submit(actor, thread.id, question)
    assert original.model_request.reasoning_effort == "high"
    interactions.save_preferences(actor, preference_request())
    assert service.submit(actor, thread.id, question) == original
    second = service.submit(
        actor, thread.id, question.model_copy(update={"idempotency_key": "deep-question-002"})
    )
    assert second.model_request.reasoning_effort == "medium"
    assert second.model_request.model == "gpt-5.4-mini"
    assert "saved preference" in second.profile.reason
    manual = service.submit(
        actor,
        thread.id,
        question.model_copy(update={"profile": "deep", "idempotency_key": "deep-question-003"}),
    )
    assert manual.model_request.reasoning_effort == "high"
    assert len(model.requests) == 3


def test_feedback_can_change_clear_and_survive_replays(model_setup):
    service, actor, model, thread = model_setup
    interactions = InteractionService(service.store, service.audit)
    run = service.submit(actor, thread.id, body())
    later = service.submit(actor, thread.id, body("another-answer"))
    request = feedback_request()
    first = interactions.save_feedback(actor, run.id, request)
    assert interactions.save_feedback(actor, run.id, request) == first
    second = interactions.save_feedback(
        actor,
        run.id,
        feedback_request(
            rating="too_slow",
            expected_version=1,
            idempotency_key="feedback-002",
        ),
    )
    assert second.version == 2
    cleared = interactions.save_feedback(
        actor,
        run.id,
        feedback_request(
            rating=None,
            expected_version=2,
            idempotency_key="feedback-003",
        ),
    )
    assert cleared.rating is None and cleared.version == 3
    assert interactions.save_feedback(actor, run.id, request) == cleared
    assert interactions.answers(actor, thread.id, 0, 1)[0].feedback == cleared
    assert interactions.answers(actor, thread.id, 1, 1)[0].run_id == later.id
    assert interactions.answers(actor, thread.id, 2, 1) == ()
    other = actor.model_copy(update={"actor_id": uuid4()})
    service.store.put_membership(
        Membership(actor_id=other.actor_id, household_id=actor.household_id, role="member")
    )
    assert interactions.answers(other, thread.id)[0].feedback is None
    interactions.save_feedback(other, run.id, request)
    assert interactions.answers(actor, thread.id)[0].feedback == cleared
    with pytest.raises(InvalidTransitionError):
        interactions.save_feedback(
            actor, run.id, feedback_request(idempotency_key="feedback-stale")
        )
    with pytest.raises(NotFoundError):
        interactions.save_feedback(
            actor.model_copy(update={"household_id": uuid4()}), run.id, request
        )
    with pytest.raises(NotFoundError):
        interactions.answers(actor.model_copy(update={"household_id": uuid4()}), thread.id)
    assert service.run(actor, run.id) == run and len(model.requests) == 2


def test_interaction_writes_rollback_with_audit(model_setup, monkeypatch):
    service, actor, _model, thread = model_setup
    interactions = InteractionService(service.store, service.audit)
    run = service.submit(actor, thread.id, body())
    before = tuple(service.store.audit_events(actor.household_id))

    def fail(**kwargs):
        raise RuntimeError("audit unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(service.audit, "record", fail)
        with pytest.raises(RuntimeError):
            interactions.save_preferences(actor, preference_request())
        with pytest.raises(RuntimeError):
            interactions.save_feedback(actor, run.id, feedback_request())
    assert interactions.preferences(actor).version == 0
    assert interactions.answers(actor, thread.id)[0].feedback is None
    assert tuple(service.store.audit_events(actor.household_id)) == before
    assert interactions.save_preferences(actor, preference_request()).version == 1
    assert interactions.save_feedback(actor, run.id, feedback_request()).version == 1


def test_concurrent_feedback_edits_detect_stale_versions(model_setup):
    service, actor, _model, thread = model_setup
    interactions = InteractionService(service.store, service.audit)
    run = service.submit(actor, thread.id, body())

    def update(index):
        try:
            return interactions.save_feedback(
                actor, run.id, feedback_request(idempotency_key=f"concurrent-{index}")
            )
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(update, range(2)))
    assert sum(value is not None for value in result) == 1
    assert interactions.answers(actor, thread.id)[0].feedback.version == 1
