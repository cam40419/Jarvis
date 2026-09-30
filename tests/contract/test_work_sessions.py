from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID, uuid4

import pytest

from simon.domain.context import CreateMemory
from simon.domain.conversations import CreateThread, SubmitRun
from simon.domain.errors import ModelBusyError, NotFoundError
from simon.domain.models import JobStatus, utc_now
from simon.services.jobs import JobService
from simon.services.model_conversations import ModelConversationService
from simon.services.tasks import AssistantTaskService
from simon.services.work_sessions import WorkSessionService
from tests.contract.test_connected import connected_setup
from tests.contract.test_model_runs import FakeModel


def setup_sessions(store):
    connected, actor, token = connected_setup(store)
    model = FakeModel()
    conversations = ModelConversationService(store, connected.audit, model, connected.settings)
    tasks = AssistantTaskService(store, connected.identity, conversations)
    connected.tasks = tasks
    service = WorkSessionService(tasks)
    thread = conversations.create(
        actor, CreateThread(title="Background", idempotency_key=str(uuid4()))
    )
    return service, actor, model, thread, connected, token


def test_saved_request_survives_service_restart_and_scoped_results(store):
    service, actor, model, thread, connected, _ = setup_sessions(store)
    request = SubmitRun(text="Work while I leave", idempotency_key="background-request")
    result = service.submit(actor, thread.id, request)
    assert result["status"] == "queued"
    assert service.submit(actor, thread.id, request) == result
    replacement = WorkSessionService(
        AssistantTaskService(store, connected.identity, service.tasks.conversations)
    )
    assert replacement.tick() == 1
    saved = replacement.list(actor, thread.id)[0]
    assert saved["status"] == "succeeded" and saved["partial_text"] == "A useful answer."
    assert (
        service.tasks.conversations.messages(actor, thread.id, 0, 100)[-1].text
        == "A useful answer."
    )
    assert replacement.tick() == 0 and len(model.requests) == 1
    other = actor.model_copy(update={"actor_id": uuid4()})
    with pytest.raises(NotFoundError):
        replacement.owned(other, UUID(result["id"]))
    with pytest.raises(NotFoundError):
        JobService(store, connected.audit).get(other, UUID(result["id"]))


def test_sessions_continue_independently_and_explicit_stop(store):
    service, actor, model, thread, _, _ = setup_sessions(store)
    entered, finish = Event(), Event()
    original = model.generate_stream

    def slow(request, delta):
        delta("Saved progress")
        delta("")
        entered.set()
        assert finish.wait(10)
        return original(request, delta)

    model.generate_stream = slow
    first = service.submit(
        actor, thread.id, SubmitRun(text="First", idempotency_key="first-session")
    )
    with ThreadPoolExecutor() as pool:
        future = pool.submit(service.tick)
        assert entered.wait(5)
        try:
            assert service.list(actor, thread.id)[0]["partial_text"] == "Saved progress"
            with pytest.raises(ModelBusyError):
                service.submit(
                    actor,
                    thread.id,
                    SubmitRun(text="Duplicate", idempotency_key="duplicate-session"),
                )
            second = service.tasks.conversations.create(
                actor, CreateThread(title="Second", idempotency_key="second-thread")
            )
            assert (
                service.submit(
                    actor, second.id, SubmitRun(text="Second", idempotency_key="second-session")
                )["status"]
                == "queued"
            )
            service.cancel(actor, UUID(first["id"]))
        finally:
            finish.set()
        future.result()
    assert service.list(actor, thread.id)[0]["status"] == "cancelled"
    assert not service.tasks.conversations.messages(actor, thread.id, 0, 100)
    assert service.tick() == 1
    assert service.list(actor, second.id)[0]["status"] == "succeeded"


def test_project_context_and_recovery_preserve_saved_work(store):
    service, actor, model, thread, connected, _ = setup_sessions(store)
    project = connected.memories.create(
        actor,
        CreateMemory(
            subject="Solar",
            content="Compare bids",
            scope="personal",
            category="project",
            idempotency_key="solar-context",
        ),
    )
    item = service.submit(
        actor,
        thread.id,
        SubmitRun(text="Compare options", idempotency_key="solar-session"),
        project.id,
    )
    job = store.get_job(UUID(item["id"]))
    store.save_job(
        job.model_copy(
            update={"status": JobStatus.RUNNING, "updated_at": utc_now() - timedelta(minutes=6)}
        ),
        job.version,
    )
    service.recover()
    assert service.list(actor, project_id=project.id)[0]["status"] == "queued"
    service.tick()
    assert (
        "Solar" in model.requests[0].instructions
        and str(project.id) in model.requests[0].instructions
    )
    job = store.get_job(job.id)
    store.save_job(
        job.model_copy(
            update={"status": JobStatus.RUNNING, "updated_at": utc_now() - timedelta(minutes=6)}
        ),
        job.version,
    )
    service.recover()
    assert service.list(actor, thread.id)[0]["status"] == "succeeded"
    assert len(model.requests) == 1


def test_interrupted_sessions_preserve_progress_without_replaying_actions(store):
    service, actor, model, thread, _, _ = setup_sessions(store)
    item = service.submit(
        actor, thread.id, SubmitRun(text="Act once", idempotency_key="act-once-request")
    )
    job = store.get_job(UUID(item["id"]))
    store.save_job(job.model_copy(update={
        "status": JobStatus.RUNNING,
        "updated_at": utc_now() - timedelta(minutes=6),
        "input": {**job.input, "run_id": str(uuid4())},
        "result": {"text": "Action was started"},
    }), job.version)
    assert service.tick() == 0
    saved = service.list(actor, thread.id)[0]
    assert saved["status"] == "failed"
    assert saved["partial_text"] == "Action was started"
    assert "review completed actions" in saved["error"]
    assert not model.requests


def test_task_recovery_requeues_unstarted_and_pauses_uncertain_work(store):
    from simon.domain.tasks import CreateAssistantTask

    service, actor, model, _, _, _ = setup_sessions(store)
    task = service.tasks.create(actor, CreateAssistantTask(
        title="Recover task", instructions="Write a summary", task_type="research",
        idempotency_key="recover-task",
    ))
    job = store.get_job(task.id)
    store.save_job(job.model_copy(update={
        "status": JobStatus.RUNNING, "updated_at": utc_now() - timedelta(minutes=11),
    }), job.version)
    service.tasks.recover_interrupted()
    job = store.get_job(task.id)
    assert job.status == JobStatus.QUEUED
    store.save_job(job.model_copy(update={
        "status": JobStatus.RUNNING, "updated_at": utc_now() - timedelta(minutes=11),
        "input": {**job.input, "run_id": str(uuid4())},
    }), job.version)
    service.tasks.recover_interrupted()
    assert store.get_job(task.id).status == JobStatus.WAITING
    assert not model.requests
