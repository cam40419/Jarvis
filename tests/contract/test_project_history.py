"""Existing run history remains paginated and isolated in memory and PostgreSQL."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from simon.domain.agent_platform import (
    AgentProfile,
    AgentTeamPlan,
    PlannedAgentTask,
    PlatformManifest,
    TeamTemplate,
)
from simon.domain.agent_runs import AgentRun, TaskExecution
from simon.domain.artifacts import Artifact
from simon.domain.errors import AuthorizationError, NotFoundError, ValidationError
from simon.domain.identity import DEV_ACTOR_ID, DEV_HOUSEHOLD_ID, Membership
from simon.domain.models import ActorContext, Channel, Job, JobStatus
from simon.services.agent_platform import AgentPlatformService
from simon.services.agent_runs import AgentRunService
from simon.services.project_history import ProjectHistoryService
from simon.services.project_work import ProjectWorkService

SAVED_AT = datetime(2026, 9, 1, 12, tzinfo=UTC)
PRIVATE_TEXT = "Private output, prompt and provider credential never in the listing"


def saved_run(
    store,
    project_id,
    *,
    actor=None,
    number=1,
    when=SAVED_AT,
    phase="planning",
    cycle_id=None,
    context_id=None,
    state_dir=None,
):
    """Pre-index durable storage format; no coordinator, provider or backfill required."""
    actor = actor or ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    run_id, task_id, plan_id = UUID(int=number), uuid4(), uuid4()
    cycle_id = cycle_id or uuid4()
    key = f"project:{cycle_id}:{phase}" if phase else "manual-project-run"
    plan = AgentTeamPlan(
        id=plan_id,
        workspace_id=actor.household_id,
        actor_id=actor.actor_id,
        team_id="studio",
        team_version=1,
        project_id=project_id,
        context_id=context_id,
        manifest_digest="0" * 64,
        max_parallel=1,
        state="planned",
        tasks=(
            PlannedAgentTask(
                id="lead-plan" if phase == "planning" else "draft",
                agent_id="writer",
                task_id=task_id,
                attempt_id=uuid4(),
                objective=PRIVATE_TEXT,
                depends_on=(),
                tool_ids=(),
            ),
        ),
        waves=(),
    )
    artifact = Artifact(
        id=uuid4(),
        workspace_id=actor.household_id,
        actor_id=actor.actor_id,
        run_id=run_id,
        task_id=task_id,
        name="report.txt",
        media_type="text/plain",
        size=6,
        sha256="0" * 64,
        created_at=when,
    )
    if state_dir:
        from simon.services.artifacts import ArtifactStore

        artifact = ArtifactStore(state_dir / "artifacts").publish_text(
            workspace_id=actor.household_id,
            actor_id=actor.actor_id,
            run_id=run_id,
            task_id=task_id,
            text="Result",
            name="report.txt",
        )
    state = AgentRun(
        id=run_id,
        plan_id=plan_id,
        workspace_id=actor.household_id,
        actor_id=actor.actor_id,
        status=JobStatus.SUCCEEDED,
        started_at=when,
        finished_at=when + timedelta(seconds=5),
        tasks=(
            TaskExecution(
                id=plan.tasks[0].id,
                agent_id="writer",
                status="succeeded",
                output=PRIVATE_TEXT,
                artifacts=(artifact,),
                events=({"private": PRIVATE_TEXT},),
            ),
        ),
    )
    store.create_job(
        Job(
            id=plan.id,
            household_id=actor.household_id,
            created_by=actor.actor_id,
            kind="platform.plan",
            idempotency_key=plan.id.hex,
            input_digest="0" * 64,
            input={
                "plan": plan.model_dump(mode="json"),
                "request": {"idempotency_key": key},
                "configuration": {"secret": PRIVATE_TEXT},
            },
            created_at=when,
        )
    )
    store.create_job(
        Job(
            id=run_id,
            household_id=actor.household_id,
            created_by=actor.actor_id,
            kind="platform.run",
            idempotency_key=run_id.hex,
            input_digest="0" * 64,
            input={"plan_id": str(plan.id), "initial_state": state.model_dump(mode="json")},
            status=state.status,
            created_at=when,
        )
    )
    return state


@pytest.fixture
def history(store, tmp_path):
    actor = ActorContext(
        actor_id=DEV_ACTOR_ID,
        household_id=DEV_HOUSEHOLD_ID,
        channel=Channel.API,
        scopes=frozenset({"jobs:read", "jobs:write"}),
    )
    project_ids = (uuid4(), uuid4())
    visible = {"allowed": True}

    def resolve(current, identifier):
        if (
            not visible["allowed"]
            or identifier not in project_ids
            or (current.actor_id, current.household_id) != (actor.actor_id, actor.household_id)
        ):
            raise NotFoundError("Project not found")
        return SimpleNamespace(id=identifier)

    team = TeamTemplate(id="studio", name="Studio", agent_ids=("writer",))
    platform = AgentPlatformService(
        store,
        PlatformManifest(
            agents=(AgentProfile(id="writer", instructions="Write."),),
            teams=(team,),
        ),
        state_dir=tmp_path,
        environ={},
    )
    platform.project_team_resolver = lambda current, identifier: (
        resolve(current, identifier) and team
    )
    work = ProjectWorkService(store, project_resolver=resolve)
    runs = AgentRunService(platform)
    return SimpleNamespace(
        actor=actor,
        projects=project_ids,
        visible=visible,
        work=work,
        runs=runs,
        store=store,
        service=ProjectHistoryService(work, runs),
    )


def test_all_older_cycles_with_stable_tied_time_cursor_and_concurrent_new_run(history, monkeypatch):
    h = history
    cycle_id = uuid4()
    for number in range(1, 106):
        saved_run(
            h.store,
            h.projects[0],
            number=number,
            cycle_id=cycle_id,
            phase="planning" if number % 2 else "execution",
        )
    saved_run(h.store, h.projects[1], number=400)
    # History must not depend on the generic 100-item listing or a global scan in services.
    monkeypatch.setattr(h.store, "jobs", lambda *_: pytest.fail("Unscoped job listing used"))
    first = h.service.list(h.actor, h.projects[0], limit=13)
    assert len(first.items) == 13 and first.next_cursor
    saved_run(h.store, h.projects[0], number=200, when=SAVED_AT + timedelta(days=1))
    items = list(first.items)
    cursor = first.next_cursor
    while cursor:
        page = h.service.list(h.actor, h.projects[0], limit=13, cursor=cursor)
        items.extend(page.items)
        cursor = page.next_cursor
    assert [row.id.int for row in items] == list(range(105, 0, -1))
    assert {row.cycle_id for row in items} == {cycle_id}
    assert {row.phase for row in items} == {"planning", "execution"}
    assert items[0].finished_at == SAVED_AT + timedelta(seconds=5)
    assert items[0].artifact_count == items[0].task_count == 1
    assert items[0].tasks[0].status == "succeeded"
    assert PRIVATE_TEXT not in first.model_dump_json()
    assert "artifacts" not in first.items[0].model_dump()


def test_project_actor_household_and_plan_join_boundaries(history):
    h = history
    good = saved_run(h.store, h.projects[0], number=1)
    saved_run(h.store, h.projects[1], number=2)
    for number, changes in ((3, {"actor_id": uuid4()}), (4, {"household_id": uuid4()})):
        other = h.actor.model_copy(update=changes)
        h.store.put_membership(
            Membership(
                actor_id=other.actor_id,
                household_id=other.household_id,
                role="owner",
            )
        )
        foreign = saved_run(h.store, h.projects[0], actor=other, number=number)
        # A run forged under our ownership must still not join another owner's plan.
        forged_id = uuid4()
        h.store.create_job(
            Job(
                id=forged_id,
                household_id=h.actor.household_id,
                created_by=h.actor.actor_id,
                kind="platform.run",
                idempotency_key=forged_id.hex,
                input_digest="0" * 64,
                input={"plan_id": str(foreign.plan_id), "initial_state": {}},
            )
        )
        assert (
            h.store.project_run_jobs(
                other.household_id,
                other.actor_id,
                h.projects[0],
                None,
                20,
            )[0].id
            == foreign.id
        )
        with pytest.raises(NotFoundError):
            h.service.list(other, h.projects[0])
    assert [row.id for row in h.service.list(h.actor, h.projects[0]).items] == [good.id]
    h.visible["allowed"] = False
    with pytest.raises(NotFoundError):
        h.service.list(h.actor, h.projects[0])


def test_cursor_validation_and_read_permission(history):
    h = history
    for number in (1, 2):
        saved_run(h.store, h.projects[0], number=number)
    first = h.service.list(h.actor, h.projects[0], limit=1)
    with pytest.raises(ValidationError, match="cursor"):
        h.service.list(h.actor, h.projects[1], cursor=first.next_cursor)
    for cursor in ("???", "a", "e30", "x" * 513):
        with pytest.raises(ValidationError, match="cursor"):
            h.service.list(h.actor, h.projects[0], cursor=cursor)
    for limit in (0, 51):
        with pytest.raises(ValidationError, match="page size"):
            h.service.list(h.actor, h.projects[0], limit=limit)
    with pytest.raises(AuthorizationError):
        h.service.list(h.actor.model_copy(update={"scopes": frozenset()}), h.projects[0])


def test_hidden_context_does_not_leak_and_cursor_can_advance_past_hidden_page(history):
    h = history
    saved_run(h.store, h.projects[0], number=1, phase=None)
    saved_run(h.store, h.projects[0], number=2, context_id="revoked")
    first = h.service.list(h.actor, h.projects[0], limit=1)
    assert not first.items and first.next_cursor
    second = h.service.list(h.actor, h.projects[0], limit=1, cursor=first.next_cursor)
    assert second.items[0].id.int == 1 and second.items[0].phase == "run"
    assert second.items[0].cycle_id is None and second.next_cursor is None
