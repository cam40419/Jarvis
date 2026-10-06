from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon.domain.errors import (
    AuthorizationError,
    IdempotencyConflictError,
    InvalidTransitionError,
    NotFoundError,
    ValidationError,
)
from simon.domain.identity import DEV_ACTOR_ID, DEV_WORKSPACE_ID
from simon.domain.models import ActorContext, Channel, Job, JobStatus
from simon.domain.project_work import (
    ConfigureProjectWork,
    ProjectActivityDraft,
    ProjectAutonomy,
    ProjectCycleUpdate,
    ProjectTeam,
    ProjectTodo,
    ProjectWorkControl,
)
from simon.services.canonical import digest
from simon.services.project_autonomy import ProjectAutonomyService
from simon.services.project_work import WORK_KIND, ProjectWorkService


@pytest.fixture
def project_work(store):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        workspace_id=DEV_WORKSPACE_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    project_id = uuid4()
    clock = [datetime(2026, 9, 30, 12, tzinfo=UTC)]
    permissions = {"visible": True, "write": True}

    def resolve(current, identifier):
        if not permissions["visible"] or (current.actor_id, current.workspace_id, identifier) != (
            actor.actor_id,
            actor.workspace_id,
            project_id,
        ):
            raise NotFoundError("Project not found")
        return SimpleNamespace(id=project_id)

    def validate(current, team):
        if set(team.agent_ids) - {"lead", "writer"} or team.max_parallel > 3:
            raise ValidationError("Project team is unavailable")

    def live_actor(*_):
        return (
            actor
            if permissions["write"]
            else actor.model_copy(update={"scopes": frozenset({"jobs:read"})})
        )

    def reconstruct():
        return ProjectWorkService(
            store,
            project_resolver=resolve,
            actor_resolver=live_actor,
            team_validator=validate,
            clock=lambda: clock[0],
        )

    work = reconstruct()
    team = ProjectTeam(name="Project studio", agent_ids=("lead", "writer"), lead_agent_id="lead")
    state = work.configure(actor, project_id, ConfigureProjectWork(expected_version=0, team=team))
    return SimpleNamespace(
        actor=actor,
        project_id=project_id,
        store=store,
        work=work,
        clock=clock,
        permissions=permissions,
        team=team,
        state=state,
        reconstruct=reconstruct,
    )


def test_manual_planning_retry_is_durable_and_claimed_once(project_work):
    h = project_work
    original = "Assess our source files and prepare a launch plan. " + "Detail. " * 200
    state = h.work.request_cycle(h.actor, h.project_id, original, "retry-original")
    previous_id = state.active_cycle.id
    state = h.work.update_cycle_atomic(
        h.actor,
        h.project_id,
        previous_id,
        lambda current: ProjectCycleUpdate(
            cycle=current.active_cycle.model_copy(
                update={"phase": "blocked", "error": "File access is missing"}
            ),
            todos=(
                ProjectTodo(
                    id="prepare-launch",
                    title="Launch plan",
                    objective="Assess the source files",
                    status="blocked",
                    cycle_id=previous_id,
                ),
            ),
        ),
    )
    work = h.reconstruct()
    work.cycle_retry_validator = lambda *_: None
    request = ProjectWorkControl(action="retry", expected_version=state.version)

    def retry():
        try:
            return work.control(h.actor, h.project_id, request)
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: retry(), range(2)))
    assert sum(result is not None for result in results) == 1
    saved = h.reconstruct().get(h.actor, h.project_id)
    assert saved.active_cycle.phase == "starting"
    assert saved.active_cycle.id != previous_id and saved.cycle_count == 2
    assert saved.active_cycle.instruction == original == saved.last_instruction
    assert saved.last_cycle.id == previous_id
    assert saved.todos[0].status == "ready"
    assert not saved.autonomy.paused and saved.blocked_reasons == ()


def test_continue_uses_durable_substantive_request_after_review(project_work):
    h = project_work
    original = "Review the brand's existing files and identify the next deliverable."
    state = h.work.request_cycle(h.actor, h.project_id, original, "continue-original")
    state = h.work.update_cycle_atomic(
        h.actor,
        h.project_id,
        state.active_cycle.id,
        lambda current: ProjectCycleUpdate(
            cycle=current.active_cycle.model_copy(
                update={"phase": "blocked", "error": "Access needed"}
            )
        ),
    )
    state = h.work.control(
        h.actor,
        h.project_id,
        ProjectWorkControl(
            action="acknowledge", expected_version=state.version, note="Access supplied"
        ),
    )
    state = h.work.control(
        h.actor, h.project_id, ProjectWorkControl(action="resume", expected_version=state.version)
    )
    state = h.reconstruct().request_cycle(
        h.actor, h.project_id, "Continue.", "continue-original-again"
    )
    assert state.active_cycle.instruction == original == state.last_instruction


def failed_planning(h, *, phase="blocked", execution_run_id=None):
    state = h.work.request_cycle(h.actor, h.project_id, "Original request", "replace-original")
    return h.work.update_cycle_atomic(
        h.actor,
        h.project_id,
        state.active_cycle.id,
        lambda current: ProjectCycleUpdate(
            cycle=current.active_cycle.model_copy(
                update={
                    "phase": phase,
                    "error": "Planning could not finish",
                    "execution_run_id": execution_run_id,
                }
            ),
            activity=(ProjectActivityDraft(kind="blocked", text="Preserve the failed attempt."),),
        ),
    )


def test_replacement_request_preserves_history_and_replays_once(project_work):
    h = project_work
    before = failed_planning(h)
    reviewed = []
    h.work.cycle_retry_validator = lambda actor, cycle: reviewed.append(cycle.id)
    state = h.work.request_cycle(
        h.actor,
        h.project_id,
        "New deliverable",
        "replace-failed-plan",
        replace_failed=True,
        expected_version=before.version,
    )
    assert state.active_cycle.instruction == state.last_instruction == "New deliverable"
    assert state.active_cycle.id != before.last_cycle.id
    assert not state.active_cycle.automatic and not state.active_cycle.execution_approved
    assert state.last_cycle == before.last_cycle
    assert state.todos == before.todos and state.team == before.team
    assert not state.blocked_reasons and not state.autonomy.paused
    assert state.cycle_count == before.cycle_count + 1
    assert reviewed == [before.last_cycle.id]
    history = h.work.list_activity(h.actor, h.project_id)["items"]
    assert sum("Replaced failed planning" in item["text"] for item in history) == 1
    assert any(item["text"] == "Preserve the failed attempt." for item in history)
    restored = h.reconstruct()
    assert (
        restored.request_cycle(
            h.actor,
            h.project_id,
            "New deliverable",
            "replace-failed-plan",
            replace_failed=True,
            expected_version=before.version,
        )
        == state
    )
    assert restored.list_activity(h.actor, h.project_id)["items"] == history
    for instruction, replace in (("Changed deliverable", True), ("New deliverable", False)):
        with pytest.raises(IdempotencyConflictError):
            h.work.request_cycle(
                h.actor,
                h.project_id,
                instruction,
                "replace-failed-plan",
                replace_failed=replace,
                expected_version=before.version,
            )


def test_replacement_request_version_race_starts_only_one_cycle(project_work):
    h = project_work
    before = failed_planning(h)
    h.work.cycle_retry_validator = lambda *_: None

    def replace(number):
        try:
            return h.work.request_cycle(
                h.actor,
                h.project_id,
                f"Replacement {number}",
                f"replacement-race-{number}",
                replace_failed=True,
                expected_version=before.version,
            )
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace, range(2)))
    assert sum(result is not None for result in results) == 1
    saved = h.reconstruct().get(h.actor, h.project_id)
    assert saved.cycle_count == before.cycle_count + 1
    assert saved.last_cycle == before.last_cycle
    assert (
        sum(
            "Replaced failed planning" in item["text"]
            for item in h.work.list_activity(h.actor, h.project_id)["items"]
        )
        == 1
    )


@pytest.mark.parametrize(
    "failure",
    [
        "automatic",
        "unversioned",
        "stale",
        "not_explicit",
        "active",
        "unknown",
        "executed",
        "no_validator",
        "review_rejected",
        "read_only",
        "foreign_actor",
        "team_revoked",
    ],
)
def test_replacement_request_cannot_bypass_review_or_permissions(project_work, failure):
    h = project_work
    before = (
        h.work.request_cycle(h.actor, h.project_id, "In progress", "already-active")
        if failure == "active"
        else failed_planning(
            h,
            phase="unknown" if failure == "unknown" else "blocked",
            execution_run_id=uuid4() if failure == "executed" else None,
        )
    )
    h.work.cycle_retry_validator = lambda *_: None
    actor = h.actor
    kwargs = {"replace_failed": True, "expected_version": before.version}
    if failure == "automatic":
        kwargs["automatic"] = True
    elif failure == "unversioned":
        kwargs.pop("expected_version")
    elif failure == "stale":
        kwargs["expected_version"] = before.version - 1
    elif failure == "not_explicit":
        kwargs["replace_failed"] = False
    elif failure == "no_validator":
        h.work.cycle_retry_validator = None
    elif failure == "read_only":
        actor = actor.model_copy(update={"scopes": frozenset({"jobs:read"})})
    elif failure == "foreign_actor":
        actor = actor.model_copy(update={"actor_id": uuid4()})
    elif failure in {"review_rejected", "team_revoked"}:

        def reject(*_):
            raise InvalidTransitionError("Current grants or outcome require review")

        if failure == "review_rejected":
            h.work.cycle_retry_validator = reject
        else:
            h.work.team_validator = reject
    history = h.work.list_activity(h.actor, h.project_id)
    with pytest.raises(
        (AuthorizationError, InvalidTransitionError, ValidationError, NotFoundError)
    ):
        h.work.request_cycle(actor, h.project_id, "Replacement", "denied-replacement", **kwargs)
    assert h.work.get(h.actor, h.project_id) == before
    assert h.work.list_activity(h.actor, h.project_id) == history


def test_replacement_request_rolls_back_acknowledgement_and_idempotency(project_work, monkeypatch):
    h = project_work
    before = failed_planning(h)
    h.work.cycle_retry_validator = lambda *_: None
    history = h.work.list_activity(h.actor, h.project_id)
    original = h.work._activity

    def interrupted(actor, state, entry, key):
        if entry.kind == "cycle":
            raise RuntimeError("Request persistence interrupted")
        return original(actor, state, entry, key)

    monkeypatch.setattr(h.work, "_activity", interrupted)
    with pytest.raises(RuntimeError, match="persistence interrupted"):
        h.work.request_cycle(
            h.actor,
            h.project_id,
            "Replacement",
            "atomic-replacement",
            replace_failed=True,
            expected_version=before.version,
        )
    assert h.work.get(h.actor, h.project_id) == before
    assert h.work.list_activity(h.actor, h.project_id) == history
    monkeypatch.setattr(h.work, "_activity", original)
    saved = h.work.request_cycle(
        h.actor,
        h.project_id,
        "Replacement",
        "atomic-replacement",
        replace_failed=True,
        expected_version=before.version,
    )
    assert saved.active_cycle.instruction == "Replacement"


class Coordinator:
    """Real atomic DB enqueues; no model or external transport is used by these tests."""

    def __init__(self, h):
        self.h = h
        self.enqueued = []
        self.fail_after_enqueue = False
        self.complete = False

    def begin(self, actor, project_id, cycle):
        def operation(state):
            current = state.active_cycle
            if current.phase != "starting":
                raise InvalidTransitionError("Cycle was already started")
            run = Job(
                workspace_id=actor.workspace_id,
                created_by=actor.actor_id,
                kind="platform.project_test_run",
                idempotency_key=str(current.id),
                input={"project_id": str(project_id)},
                input_digest=digest(str(project_id)),
            )
            self.h.store.create_job(run)
            self.enqueued.append(run.id)
            return ProjectCycleUpdate(
                cycle=current.model_copy(
                    update={
                        "phase": "planning",
                        "planning_plan_id": uuid4(),
                        "planning_run_id": run.id,
                        "model_reserved_usd": 0.1,
                    }
                )
            )

        self.h.work.update_cycle_atomic(actor, project_id, cycle.id, operation)
        if self.fail_after_enqueue:
            raise RuntimeError("Connection lost after durable enqueue")
        return None

    def advance(self, actor, project_id, cycle):
        if self.complete:
            return ProjectCycleUpdate(
                cycle=cycle.model_copy(update={"phase": "completed"}),
                activity=(ProjectActivityDraft(kind="finding", text="Saved research result."),),
            )
        return None


def test_initial_records_activity_and_archive_survive_reconstruction(project_work):
    h = project_work
    h.work.record_activity(
        h.actor,
        h.project_id,
        ProjectActivityDraft(kind="finding", text="Keep this finding."),
        idempotency_key="finding-persist",
    )
    state = h.work.add_todo(
        h.actor,
        h.project_id,
        ProjectTodo(id="finished", title="Finish", objective="Draft", status="done"),
        idempotency_key="finished-todo",
    )
    state = h.work.update_todo(
        h.actor,
        h.project_id,
        state.todos[0].model_copy(update={"status": "archived"}),
        expected_version=state.version,
    )
    restored = h.reconstruct()
    assert not restored.get(h.actor, h.project_id).todos
    history = restored.list_activity(h.actor, h.project_id)["items"]
    assert history[0]["sequence"] == state.activity_count
    assert any(item["text"] == "Keep this finding." for item in history)
    assert restored.list_archived(h.actor, h.project_id)["items"][0]["id"] == "finished"
    initial = h.store.get_job(h.work.identifier(h.actor, h.project_id)).input["initial_state"]
    assert initial["project_id"] == str(h.project_id)


def test_todo_idempotency_conflicts_and_graph_rollback(project_work):
    h = project_work

    def todo():
        return ProjectTodo(title="Research", objective="Review source material.")

    first = h.work.add_todo(h.actor, h.project_id, todo(), idempotency_key="add-default-todo")
    repeated = h.work.add_todo(h.actor, h.project_id, todo(), idempotency_key="add-default-todo")
    assert repeated == first and len(repeated.todos) == 1
    with pytest.raises(IdempotencyConflictError):
        h.work.add_todo(
            h.actor,
            h.project_id,
            ProjectTodo(title="Changed", objective="Different"),
            idempotency_key="add-default-todo",
        )
    for invalid in (
        ProjectTodo(id="missing-dep", title="Invalid", objective="Invalid", depends_on=("absent",)),
        ProjectTodo(id="wrong-agent", title="Invalid", objective="Invalid", agent_id="outsider"),
        ProjectTodo(
            id="self-cycle", title="Invalid", objective="Invalid", depends_on=("self-cycle",)
        ),
    ):
        with pytest.raises(ValidationError):
            h.work.add_todo(h.actor, h.project_id, invalid, idempotency_key="invalid-" + invalid.id)
    assert h.work.get(h.actor, h.project_id) == first
    with pytest.raises(IdempotencyConflictError):
        h.work.record_activity(
            h.actor,
            h.project_id,
            ProjectActivityDraft(text="Changed configuration record."),
            idempotency_key="activity-conflict",
        )
        h.work.record_activity(
            h.actor,
            h.project_id,
            ProjectActivityDraft(text="Different"),
            idempotency_key="activity-conflict",
        )


def test_history_pagination_is_newest_first_and_owner_scoped(project_work):
    h = project_work
    for index in range(6):
        h.work.record_activity(
            h.actor,
            h.project_id,
            ProjectActivityDraft(text=f"Observation {index}"),
            idempotency_key=f"observation-{index}",
        )
    first = h.work.list_activity(h.actor, h.project_id, limit=3)
    second = h.work.list_activity(h.actor, h.project_id, offset=first["next_offset"], limit=3)
    assert [item["text"] for item in first["items"]] == [f"Observation {i}" for i in (5, 4, 3)]
    assert [item["text"] for item in second["items"]] == [f"Observation {i}" for i in (2, 1, 0)]
    for stranger in (
        h.actor.model_copy(update={"actor_id": uuid4()}),
        h.actor.model_copy(update={"workspace_id": uuid4()}),
    ):
        with pytest.raises(NotFoundError):
            h.work.get(stranger, h.project_id)
        with pytest.raises(NotFoundError):
            h.work.list_activity(stranger, h.project_id)
    with pytest.raises(AuthorizationError):
        h.work.record_activity(
            h.actor.model_copy(update={"scopes": frozenset({"jobs:read"})}),
            h.project_id,
            ProjectActivityDraft(text="Forbidden"),
            idempotency_key="forbidden-note",
        )


def test_two_schedulers_enqueue_one_run_and_restart_never_duplicates(project_work):
    h = project_work
    h.work.request_cycle(h.actor, h.project_id, "Research the next step", "initial-cycle")
    coordinator = Coordinator(h)
    schedulers = [ProjectAutonomyService(h.work, coordinator, enabled=True) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda scheduler: scheduler.tick(), schedulers))
    assert len(coordinator.enqueued) == 1
    restored = h.reconstruct()
    scheduler = ProjectAutonomyService(restored, coordinator, enabled=True)
    assert scheduler.tick() == 0
    state = restored.get(h.actor, h.project_id)
    assert state.active_cycle.planning_run_id == coordinator.enqueued[0]
    assert (
        len(
            h.store.jobs(h.actor.workspace_id, h.actor.actor_id, "platform.project_test_run", 0, 10)
        )
        == 1
    )


def test_failed_atomic_update_rolls_back_the_run_and_event(project_work):
    h = project_work
    state = h.work.request_cycle(h.actor, h.project_id, "Research", "atomic-cycle")
    identifier = uuid4()

    def operation(current):
        h.store.create_job(
            Job(
                id=identifier,
                workspace_id=h.actor.workspace_id,
                created_by=h.actor.actor_id,
                kind="platform.project_test_run",
                idempotency_key=identifier.hex,
                input={},
                input_digest=digest({}),
            )
        )
        return ProjectCycleUpdate(
            cycle=current.active_cycle.model_copy(
                update={
                    "phase": "planning",
                    "planning_run_id": identifier,
                    # Omit required planning_plan_id; the entire transaction must roll back.
                }
            ),
            activity=(ProjectActivityDraft(text="Must not survive"),),
        )

    with pytest.raises(ValidationError):
        h.work.update_cycle_atomic(h.actor, h.project_id, state.active_cycle.id, operation)
    assert h.store.get_job(identifier) is None
    assert h.work.get(h.actor, h.project_id) == state


def test_callback_interruption_preserves_link_and_requires_recovery(project_work):
    h = project_work
    h.work.request_cycle(h.actor, h.project_id, "Research", "interrupted-cycle")
    coordinator = Coordinator(h)
    coordinator.fail_after_enqueue = True
    scheduler = ProjectAutonomyService(h.work, coordinator, enabled=True)
    assert scheduler.tick() == 1
    state = h.work.get(h.actor, h.project_id)
    assert state.active_cycle is None and state.last_cycle.phase == "unknown"
    assert state.last_cycle.planning_run_id == coordinator.enqueued[0]
    assert state.autonomy.paused and state.blocked_reasons
    assert ProjectAutonomyService(h.reconstruct(), coordinator, enabled=True).tick() == 0
    with pytest.raises(InvalidTransitionError):
        h.work.control(
            h.actor,
            h.project_id,
            ProjectWorkControl(
                expected_version=state.version,
                action="acknowledge",
                note="I looked at it.",
            ),
        )
    recovered = []
    h.work.cycle_recovery_validator = lambda actor, cycle: recovered.append(cycle.planning_run_id)
    state = h.work.control(
        h.actor,
        h.project_id,
        ProjectWorkControl(
            expected_version=state.version,
            action="acknowledge",
            note="Operator settled the linked run.",
        ),
    )
    assert recovered == coordinator.enqueued and state.autonomy.paused
    with pytest.raises(InvalidTransitionError):
        h.work.request_cycle(h.actor, h.project_id, "Retry", "paused-retry")


def test_scheduling_cadence_limit_and_saved_objective(project_work):
    h = project_work
    coordinator = Coordinator(h)
    coordinator.complete = True
    state = h.work.configure(
        h.actor,
        h.project_id,
        ConfigureProjectWork(
            expected_version=h.state.version,
            autonomy=ProjectAutonomy(
                mode="scheduled",
                objective="Review the standing backlog.",
                cadence_minutes=5,
                max_cycles=1,
                model_budget_usd=0.5,
            ),
        ),
    )
    scheduler = ProjectAutonomyService(h.work, coordinator, enabled=True)
    assert scheduler.tick() == 0
    h.clock[0] += timedelta(minutes=5)
    assert ProjectAutonomyService(h.work, coordinator).tick() == 0  # Disabled by default.
    assert scheduler.tick() == 1  # Due cycle only; no callback in this pass.
    state = h.work.get(h.actor, h.project_id)
    assert state.active_cycle.instruction == state.autonomy.objective
    assert state.active_cycle.automatic and state.scheduled_cycles_used == 1
    assert scheduler.tick() == 1  # Atomic lead run.
    assert scheduler.tick() == 1  # Completion + durable finding.
    finished = h.work.get(h.actor, h.project_id)
    assert finished.last_cycle.phase == "completed"
    assert finished.next_cycle_at == h.clock[0] + timedelta(minutes=5)
    assert scheduler.tick() == 0
    h.clock[0] += timedelta(minutes=5)
    assert scheduler.tick() == 1
    final = h.work.get(h.actor, h.project_id)
    assert final.cycle_count == 1 and final.autonomy.paused
    assert "cycle limit" in final.blocked_reasons[0]
    assert len(coordinator.enqueued) == 1


def test_revoked_write_access_halts_without_new_agent_run(project_work):
    h = project_work
    h.work.request_cycle(h.actor, h.project_id, "Research", "revocation-cycle")
    coordinator = Coordinator(h)
    h.permissions["write"] = False
    assert ProjectAutonomyService(h.work, coordinator, enabled=True).tick() == 1
    state = h.work.get(h.actor, h.project_id)
    assert state.last_cycle.phase == "blocked" and state.autonomy.paused
    assert not coordinator.enqueued
    job = h.store.get_job(h.work.identifier(h.actor, h.project_id))
    assert job.status == JobStatus.NEEDS_HUMAN and job.kind == WORK_KIND


def test_pause_fences_stale_results_and_atomic_enqueues(project_work):
    h = project_work
    state = h.work.request_cycle(h.actor, h.project_id, "Research", "pause-cycle")
    cycle = state.active_cycle
    paused = h.work.configure(
        h.actor,
        h.project_id,
        ConfigureProjectWork(
            expected_version=state.version,
            autonomy=state.autonomy.model_copy(update={"paused": True}),
        ),
    )
    assert paused.active_cycle.revision == cycle.revision + 1
    with pytest.raises(InvalidTransitionError):
        h.work.apply_cycle(
            h.actor,
            h.project_id,
            ProjectCycleUpdate(
                cycle=cycle.model_copy(update={"phase": "blocked", "error": "Stale error"}),
            ),
        )
    called = []
    with pytest.raises(InvalidTransitionError):
        h.work.update_cycle_atomic(
            h.actor, h.project_id, cycle.id, lambda _: called.append("must not enqueue")
        )
    assert not called
    coordinator = Coordinator(h)
    assert ProjectAutonomyService(h.work, coordinator, enabled=True).tick() == 0
    assert h.work.get(h.actor, h.project_id) == paused
    state = h.work.control(
        h.actor,
        h.project_id,
        ProjectWorkControl(
            expected_version=paused.version,
            action="resume",
        ),
    )
    assert ProjectAutonomyService(h.work, coordinator, enabled=True).tick() == 1
    assert len(coordinator.enqueued) == 1


def test_team_revision_stale_edit_and_active_cycle_immutability(project_work):
    h = project_work
    unchanged = h.work.configure(
        h.actor,
        h.project_id,
        ConfigureProjectWork(
            expected_version=h.state.version,
            team=h.team.model_copy(update={"revision": 500}),
        ),
    )
    assert unchanged.team.revision == 1
    changed = h.work.configure(
        h.actor,
        h.project_id,
        ConfigureProjectWork(
            expected_version=unchanged.version,
            team=h.team.model_copy(update={"roles": {"writer": "Prepare launch copy"}}),
        ),
    )
    assert changed.team.revision == 2
    with pytest.raises(InvalidTransitionError):
        h.work.configure(
            h.actor,
            h.project_id,
            ConfigureProjectWork(
                expected_version=unchanged.version,
                team=h.team,
            ),
        )
    state = h.work.request_cycle(h.actor, h.project_id, "Research", "team-cycle")
    for body in (
        ConfigureProjectWork(expected_version=state.version, team=h.team),
        ConfigureProjectWork(
            expected_version=state.version, autonomy=ProjectAutonomy(model_budget_usd=4)
        ),
    ):
        with pytest.raises(InvalidTransitionError):
            h.work.configure(h.actor, h.project_id, body)
    assert h.work.get(h.actor, h.project_id) == state


def test_budget_and_manual_approval_cannot_be_widened_by_coordinator(project_work):
    h = project_work
    h.work.configure(
        h.actor,
        h.project_id,
        ConfigureProjectWork(
            expected_version=h.state.version,
            autonomy=ProjectAutonomy(model_budget_usd=0.25),
        ),
    )
    h.work.request_cycle(h.actor, h.project_id, "Research", "budget-cycle")
    coordinator = Coordinator(h)
    scheduler = ProjectAutonomyService(h.work, coordinator, enabled=True)
    scheduler.tick()
    state = h.work.get(h.actor, h.project_id)
    for changes in (
        {"model_reserved_usd": 0.3},
        {"model_reserved_usd": 0},
        {"model_budget_usd": 10},
        {"automatic": True},
        {"planning_run_id": uuid4()},
    ):
        with pytest.raises(ValidationError):
            h.work.apply_cycle(
                h.actor,
                h.project_id,
                ProjectCycleUpdate(
                    cycle=state.active_cycle.model_copy(update=changes),
                ),
            )
    with pytest.raises(AuthorizationError):
        h.work.apply_cycle(
            h.actor,
            h.project_id,
            ProjectCycleUpdate(
                cycle=state.active_cycle.model_copy(update={"execution_approved": True}),
            ),
        )
    state = h.work.apply_cycle(
        h.actor,
        h.project_id,
        ProjectCycleUpdate(
            cycle=state.active_cycle.model_copy(
                update={"phase": "ready", "execution_plan_id": uuid4()}
            ),
        ),
    )
    with pytest.raises(AuthorizationError):
        h.work.apply_cycle(
            h.actor,
            h.project_id,
            ProjectCycleUpdate(
                cycle=state.active_cycle.model_copy(
                    update={"phase": "executing", "execution_run_id": uuid4()}
                ),
            ),
        )
    assert h.work.get(h.actor, h.project_id) == state


def test_archive_frees_full_backlog_and_preserves_unrelated_work(project_work):
    h = project_work
    rows = tuple(
        ProjectTodo(
            id=f"task-{index}", title=f"Task {index}", objective="Saved task", status="done"
        )
        for index in range(500)
    )
    job = h.store.get_job(h.work.identifier(h.actor, h.project_id))
    h.work._save(job, h.state.model_copy(update={"todos": rows}))
    with pytest.raises(ValidationError):
        h.work.add_todo(
            h.actor,
            h.project_id,
            ProjectTodo(title="Overflow", objective="Overflow"),
            idempotency_key="overflow-todo",
        )
    state = h.work.get(h.actor, h.project_id)
    state = h.work.update_todo(
        h.actor,
        h.project_id,
        rows[0].model_copy(update={"status": "archived"}),
        expected_version=state.version,
    )
    state = h.work.add_todo(
        h.actor,
        h.project_id,
        ProjectTodo(title="Next", objective="Next"),
        idempotency_key="next-task",
    )
    assert len(state.todos) == 500 and state.todos[0] == rows[1]
    assert h.work.list_archived(h.actor, h.project_id)["items"][0]["title"] == "Task 0"
    with pytest.raises(InvalidTransitionError):
        h.work.add_todo(h.actor, h.project_id, rows[0], idempotency_key="resurrect-task")


def test_paused_execution_drains_without_scheduling_new_work(project_work):
    h = project_work
    coordinator = Coordinator(h)
    h.work.request_cycle(h.actor, h.project_id, "Research", "drain-cycle")
    scheduler = ProjectAutonomyService(h.work, coordinator, enabled=True)
    scheduler.tick()
    state = h.work.get(h.actor, h.project_id)
    state = h.work.apply_cycle(
        h.actor,
        h.project_id,
        ProjectCycleUpdate(
            cycle=state.active_cycle.model_copy(
                update={"phase": "ready", "execution_plan_id": uuid4()}
            ),
        ),
    )
    state = h.work.control(
        h.actor,
        h.project_id,
        ProjectWorkControl(
            expected_version=state.version,
            action="run_ready",
        ),
    )
    state = h.work.apply_cycle(
        h.actor,
        h.project_id,
        ProjectCycleUpdate(
            cycle=state.active_cycle.model_copy(
                update={"phase": "executing", "execution_run_id": uuid4()}
            ),
        ),
    )
    state = h.work.control(
        h.actor,
        h.project_id,
        ProjectWorkControl(
            expected_version=state.version,
            action="pause",
        ),
    )
    coordinator.complete = True
    assert scheduler.tick() == 1
    state = h.work.get(h.actor, h.project_id)
    assert state.last_cycle.phase == "completed" and state.autonomy.paused
    assert scheduler.tick() == 0 and len(coordinator.enqueued) == 1


def test_clients_cannot_fabricate_run_owned_todos_or_change_execution_links(project_work):
    h = project_work
    for change in (
        {"run_id": uuid4()},
        {"plan_id": uuid4()},
        {"cycle_id": uuid4()},
        {"run_task_id": "foreign-task"},
        {"status": "running"},
        {"status": "unknown"},
        {"status": "archived"},
    ):
        todo = ProjectTodo(title="Spoofed worker", objective="Never inserted", **change)
        with pytest.raises(ValidationError):
            h.work.add_todo(h.actor, h.project_id, todo, idempotency_key="spoofed-todo")
    assert h.work.get(h.actor, h.project_id) == h.state
    state = h.work.add_todo(
        h.actor,
        h.project_id,
        ProjectTodo(id="manual", title="Manual", objective="Manual work"),
        idempotency_key="manual-todo",
    )
    for change in ({"run_id": uuid4()}, {"status": "running"}, {"status": "unknown"}):
        with pytest.raises(ValidationError):
            h.work.update_todo(
                h.actor,
                h.project_id,
                state.todos[0].model_copy(update=change),
                expected_version=state.version,
            )
    assert h.work.get(h.actor, h.project_id) == state
