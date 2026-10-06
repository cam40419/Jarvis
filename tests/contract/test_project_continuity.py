from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
)
from simon.domain.project_continuity import (
    CreateProjectSchedule,
    CreateProjectWait,
    QueueProjectRequest,
    ReplyProjectWait,
    SetProjectSchedule,
)
from simon.domain.project_work import ProjectCycleUpdate, ProjectWorkControl
from simon.services.project_continuity import ProjectContinuityService
from tests.contract.test_project_work import project_work as project_work


def queue(h, service, key="followup-one", instruction="Draft the next project update", **kwargs):
    return service.queue(
        h.actor,
        h.project_id,
        QueueProjectRequest(instruction=instruction, idempotency_key=key, **kwargs),
    )


def test_followup_survives_restart_and_two_schedulers_claim_once(project_work):
    h = project_work
    service = ProjectContinuityService(h.work)
    active = h.work.request_cycle(h.actor, h.project_id, "Current request", "active-request")
    saved = queue(h, service)
    assert queue(h, service)["id"] == saved["id"]
    assert service.tick() == 0
    h.work.control(
        h.actor, h.project_id, ProjectWorkControl(action="discard", expected_version=active.version)
    )
    recovered = ProjectContinuityService(h.reconstruct())
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: recovered.tick(), range(2)))
    state = h.work.get(h.actor, h.project_id)
    assert state.cycle_count == 2
    assert state.active_cycle.request_id == UUID(saved["id"])
    rows = recovered.snapshot(h.actor, h.project_id)["requests"]
    assert len(rows) == 1 and rows[0]["status"] == "started"
    assert recovered.tick() == 0


def test_request_idempotency_ownership_and_cancellation(project_work):
    h = project_work
    service = ProjectContinuityService(h.work)
    row = queue(h, service)
    with pytest.raises(IdempotencyConflictError):
        queue(h, service, instruction="A different request")
    with pytest.raises(NotFoundError):
        service.cancel_request(h.actor, uuid4(), UUID(row["id"]), row["version"])
    with pytest.raises(AuthorizationError):
        service.cancel_request(
            h.actor.model_copy(update={"scopes": frozenset({"jobs:read"})}),
            h.project_id,
            UUID(row["id"]),
            row["version"],
        )
    service.cancel_request(h.actor, h.project_id, UUID(row["id"]), row["version"])
    assert service.tick() == 0
    assert h.work.get(h.actor, h.project_id).active_cycle is None


def test_wait_releases_project_and_reply_is_durable_and_idempotent(project_work):
    h = project_work
    service = ProjectContinuityService(h.work)
    active = h.work.request_cycle(h.actor, h.project_id, "Draft launch schedule", "wait-original")
    # A persisted planning result requests user input; no execution was dispatched.
    active = h.work.update_cycle_atomic(
        h.actor,
        h.project_id,
        active.active_cycle.id,
        lambda state: ProjectCycleUpdate(
            cycle=state.active_cycle.model_copy(
                update={
                    "phase": "planning",
                    "planning_plan_id": uuid4(),
                    "planning_run_id": uuid4(),
                }
            )
        ),
    )
    settled = h.work.update_cycle_atomic(
        h.actor,
        h.project_id,
        active.active_cycle.id,
        lambda state: ProjectCycleUpdate(
            cycle=state.active_cycle.model_copy(
                update={"phase": "waiting", "error": "Which launch date should we use?"}
            )
        ),
    )
    assert settled.active_cycle is None and not settled.autonomy.paused
    assert settled.blocked_reasons == () and settled.last_cycle.phase == "waiting"
    wait = service.snapshot(h.actor, h.project_id)["waits"][0]
    queue(h, service, key="independent-work", instruction="Organize the existing notes")
    assert service.tick() == 1  # A question does not halt independent work.
    recovered = ProjectContinuityService(h.reconstruct())
    reply = ReplyProjectWait(
        expected_version=wait["version"], message="October 30", idempotency_key="reply-once"
    )
    first = recovered.reply(h.actor, h.project_id, UUID(wait["id"]), reply)
    assert recovered.reply(h.actor, h.project_id, UUID(wait["id"]), reply) == first
    assert first["status"] == "replied"
    with pytest.raises(NotFoundError):
        recovered.reply(h.actor, uuid4(), UUID(wait["id"]), reply)
    h.permissions["visible"] = False
    with pytest.raises(NotFoundError):
        recovered.reply(h.actor, h.project_id, UUID(wait["id"]), reply)
    h.permissions["visible"] = True
    pending = [
        row
        for row in recovered.snapshot(h.actor, h.project_id)["requests"]
        if row["status"] == "queued"
    ]
    assert len(pending) == 1
    assert (
        "Draft launch schedule" in pending[0]["instruction"]
        and "October 30" in pending[0]["instruction"]
    )
    with pytest.raises(InvalidTransitionError):
        recovered.reply(
            h.actor,
            h.project_id,
            UUID(wait["id"]),
            reply.model_copy(update={"idempotency_key": "late-reply"}),
        )


def test_calendar_missed_occurrence_coalesces_and_target_budget_is_saved(project_work):
    h = project_work
    service = ProjectContinuityService(h.work)
    schedule = service.schedule(
        h.actor,
        h.project_id,
        CreateProjectSchedule(
            name="Daily writing",
            instruction="Draft the daily report",
            kind="daily",
            timezone="UTC",
            local_time="13:00",
            agent_id="writer",
            model_budget_usd=2,
            max_runs=2,
            idempotency_key="daily-schedule",
        ),
    )
    h.clock[0] += timedelta(days=4, hours=2)
    recovered = ProjectContinuityService(h.reconstruct())
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: recovered.tick(), range(2)))
    snapshot = recovered.snapshot(h.actor, h.project_id)
    assert len(snapshot["requests"]) == 1
    assert snapshot["schedules"][0]["runs_used"] == 1
    cycle = h.work.get(h.actor, h.project_id).active_cycle
    assert cycle.target_agent_id == "writer" and cycle.model_budget_usd == 2
    assert cycle.bounded_execution and not cycle.automatic
    assert recovered.tick() == 0
    h.work.control(
        h.actor,
        h.project_id,
        ProjectWorkControl(
            action="discard", expected_version=h.work.get(h.actor, h.project_id).version
        ),
    )
    h.clock[0] += timedelta(days=1)
    recovered.tick()
    state = recovered.snapshot(h.actor, h.project_id)["schedules"][0]
    assert state["runs_used"] == 2 and not state["enabled"] and state["next_run_at"] is None
    with pytest.raises(InvalidTransitionError):
        recovered.set_schedule(
            h.actor,
            h.project_id,
            UUID(schedule["id"]),
            SetProjectSchedule(expected_version=state["version"], enabled=True),
        )


def test_queued_revoked_access_does_not_start_work(project_work):
    h = project_work
    service = ProjectContinuityService(h.work)
    queue(h, service)
    h.permissions["write"] = False
    service.tick()
    assert h.work.get(h.actor, h.project_id).active_cycle is None
    row = service.snapshot(h.actor, h.project_id)["requests"][0]
    assert row["status"] == "blocked" and row["last_error"]
    with pytest.raises(AuthorizationError):
        service.retry_request(h.actor, h.project_id, UUID(row["id"]), row["version"])
    h.permissions["write"] = True
    retried = service.retry_request(h.actor, h.project_id, UUID(row["id"]), row["version"])
    assert retried["status"] == "queued"
    assert service.tick() == 1
    assert h.work.get(h.actor, h.project_id).active_cycle.request_id == UUID(row["id"])


def test_wait_deadline_is_retained_without_starting_unanswered_work(project_work):
    h = project_work
    service = ProjectContinuityService(h.work)
    when = h.clock[0] + timedelta(days=1)
    service.create_wait(
        h.actor,
        h.project_id,
        CreateProjectWait(
            instruction="Prepare supplier comparison",
            question="Which supplier replied?",
            due_at=when,
            idempotency_key="supplier-reply",
        ),
    )
    h.clock[0] += timedelta(days=2)
    recovered = ProjectContinuityService(h.reconstruct())
    assert recovered.tick() == 0
    wait = recovered.snapshot(h.actor, h.project_id)["waits"][0]
    assert wait["due_at"] == when.isoformat() and wait["status"] == "waiting"


def test_queue_claim_conflict_rolls_back_new_cycle_and_idempotency(project_work, monkeypatch):
    h = project_work
    service = ProjectContinuityService(h.work)
    row = queue(h, service)
    original = service._save

    def fail_after_cycle(job, data, status):
        if data.get("status") == "started":
            raise InvalidTransitionError("Synthetic queue persistence conflict")
        return original(job, data, status)

    monkeypatch.setattr(service, "_save", fail_after_cycle)
    service.tick()
    state = h.reconstruct().get(h.actor, h.project_id)
    assert state.active_cycle is None and state.cycle_count == 0
    saved = service.snapshot(h.actor, h.project_id)["requests"][0]
    assert saved["id"] == row["id"] and saved["status"] == "blocked"


def test_project_pause_preserves_calendar_allowance_until_resumed(project_work):
    h = project_work
    service = ProjectContinuityService(h.work)
    service.schedule(
        h.actor,
        h.project_id,
        CreateProjectSchedule(
            name="Later report",
            instruction="Prepare the report",
            kind="once",
            run_at=h.clock[0] + timedelta(hours=1),
            model_budget_usd=1,
            max_runs=1,
            idempotency_key="paused-calendar",
        ),
    )
    paused = h.work.control(
        h.actor, h.project_id, ProjectWorkControl(action="pause", expected_version=h.state.version)
    )
    h.clock[0] += timedelta(hours=2)
    assert service.tick() == 0
    assert service.snapshot(h.actor, h.project_id)["schedules"][0]["runs_used"] == 0
    h.work.control(
        h.actor, h.project_id, ProjectWorkControl(action="resume", expected_version=paused.version)
    )
    assert service.tick() == 2  # One occurrence and one claimed request.
    assert h.work.get(h.actor, h.project_id).active_cycle.bounded_execution


def test_one_time_schedule_creation_replay_after_due_is_not_a_new_schedule(project_work):
    h = project_work
    service = ProjectContinuityService(h.work)
    body = CreateProjectSchedule(
        name="One report",
        instruction="Prepare the report",
        kind="once",
        run_at=h.clock[0] + timedelta(hours=1),
        model_budget_usd=1,
        max_runs=1,
        idempotency_key="one-schedule-replay",
    )
    original = service.schedule(h.actor, h.project_id, body)
    h.clock[0] += timedelta(hours=2)
    service.tick()
    replay = service.schedule(h.actor, h.project_id, body)
    assert replay["id"] == original["id"] and replay["runs_used"] == 1
    assert len(service.snapshot(h.actor, h.project_id)["requests"]) == 1
    h.permissions["visible"] = False
    with pytest.raises(NotFoundError):
        service.schedule(h.actor, h.project_id, body)
