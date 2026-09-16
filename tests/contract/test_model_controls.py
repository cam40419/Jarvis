from uuid import uuid4

import pytest

from simon.domain.conversations import SubmitRun
from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    ModelError,
    NotFoundError,
)
from tests.contract.test_model_runs import body
from tests.contract.test_model_runs import model_setup as model_setup


def test_auto_config_reaches_provider_and_survives_replay(model_setup):
    service, actor, model, thread = model_setup
    request = SubmitRun(
        text="Compare two ways to organize my notes. Keep it brief.",
        answer_length="auto",
        idempotency_key="auto-config-001",
    )
    run = service.submit(actor, thread.id, request)
    assert model.requests[0].reasoning_effort == "medium"
    assert model.requests[0].verbosity == "low"
    assert run.profile.version == "profiles-v2"
    service.settings = service.settings.model_copy(update={"openai_model": "gpt-5.4"})
    assert service.submit(actor, thread.id, request) == run
    assert len(model.requests) == 1


def test_profile_snapshots_and_think_deeper(model_setup):
    service, actor, model, thread = model_setup
    first = service.submit(actor, thread.id, body())
    assert first.profile.selected == "balanced"
    assert first.model_request.reasoning_effort == "low"
    deeper = SubmitRun(
        text=body().text,
        idempotency_key="deeper-request",
        parent_run_id=first.id,
        profile="deep",
        answer_length="brief",
    )
    second = service.submit(actor, thread.id, deeper)
    assert second.parent_run_id == first.id
    assert second.profile.reason == "Think deeper"
    assert second.model_request.reasoning_effort == "high"
    assert second.model_request.verbosity == "low"
    assert second.model_request.model == "gpt-5.4"
    assert second.model_request.max_output_tokens == 16384
    assert service.run(actor, first.id) == first
    assert service.store.latest_run(thread.id) == second
    assert service.submit(actor, thread.id, deeper) == second
    assert len(model.requests) == 2
    with pytest.raises(IdempotencyConflictError):
        service.submit(actor, thread.id, deeper.model_copy(update={"answer_length": "detailed"}))


def test_stream_before_commit_and_cancel_preserves_original(model_setup):
    service, actor, _model, thread = model_setup
    first = service.submit(actor, thread.id, body())
    active = []
    seen = []

    def delta(text):
        seen.append(text)
        assert len(service.messages(actor, thread.id, 0, 100)) == 2
        service.cancel(actor, active[0].id)

    with pytest.raises(ModelError, match="stopped"):
        service.submit(
            actor, thread.id, body("streaming-request"), on_started=active.append, on_delta=delta
        )
    assert seen and service.store.attempt(active[0].id).status == "failed"
    assert service.store.latest_run(thread.id) == first
    service.cancel(actor, active[0].id)
    with pytest.raises(NotFoundError):
        service.cancel(actor.model_copy(update={"household_id": uuid4()}), active[0].id)
    with pytest.raises(AuthorizationError):
        service.cancel(actor.model_copy(update={"actor_id": uuid4()}), active[0].id)


def test_disconnect_cancels_pending_stream(model_setup):
    import asyncio
    from threading import Event

    from simon.api.model_stream import model_stream

    service, actor, model, thread = model_setup
    release = Event()
    finished = Event()

    def generate_stream(request, on_delta):
        try:
            on_delta("provisional")
            assert release.wait(10)
            on_delta(" more")
            return model.generate(request)
        finally:
            finished.set()

    model.generate_stream = generate_stream

    async def scenario():
        response = model_stream(service, actor, thread.id, body(), lambda: actor)
        stream = response.body_iterator
        try:
            assert "run.started" in await anext(stream)
            assert "provisional" in await anext(stream)
            attempt = service.store.pending_attempt(thread.id)
            assert attempt is not None
            assert service.messages(actor, thread.id, 0, 100) == ()
            await stream.aclose()
            assert service.store.attempt(attempt.run.id).error_code == "model_cancelled"
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)
        assert service.messages(actor, thread.id, 0, 100) == ()

    asyncio.run(scenario())
