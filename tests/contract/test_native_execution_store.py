"""Native execution scope, durable checkpoints and concurrent admission contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_agents import NativeAgent
from simon.domain.native_execution import (
    NativeExecutionEvent,
    NativeExecutionPolicy,
    NativeExecutionRun,
    NativeExecutionRunner,
    NativeExecutionSchedule,
    NativeExecutionSignal,
    NativeExecutionStep,
    NativeExecutionWait,
    NativeTaskWorkflow,
)
from simon.domain.native_models import ProjectModel
from simon.domain.native_projects import NativeTask
from tests.contract.test_native_project_store import seed_native_projects


def seed_execution(store):
    h = seed_native_projects(store)
    h.agent = NativeAgent(
        workspace_id=h.workspace,
        project_id=h.first.id,
        role_key="writer",
        name="Writer",
        instructions="Draft carefully",
        success_criteria="Clear",
        rationale="Prepare candidates",
        created_by=h.owner,
    )
    store.insert_native_agent(h.agent)
    h.model = ProjectModel(
        workspace_id=h.workspace,
        project_id=h.first.id,
        template_id="local",
        label="Local",
        created_by=h.owner,
    )
    store.insert_project_model(h.model)
    h.task = NativeTask(
        workspace_id=h.workspace,
        project_id=h.first.id,
        title="Prepare a candidate",
        created_by=h.owner,
    )
    h.other = h.task.model_copy(update={"id": uuid4(), "title": "Review"})
    store.insert_native_task(h.task)
    store.insert_native_task(h.other)
    h.policy = NativeExecutionPolicy(
        workspace_id=h.workspace, project_id=h.first.id, version=1, issued_by=h.owner, enabled=True
    )
    store.save_execution_policy(h.policy, 0)
    return h


@pytest.fixture
def execution(store):
    return seed_execution(store)


def run_record(h, **changes):
    identifier = uuid4()
    now = utc_now()
    return NativeExecutionRun(
        **{
            "id": identifier,
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "task_id": h.task.id,
            "agent_id": h.agent.id,
            "issued_by": h.owner,
            "root_run_id": identifier,
            "task_version": 1,
            "agent_version": 1,
            "project_version": 1,
            "workflow_version": 0,
            "policy_version": 1,
            "input_digest": "a" * 64,
            "context": {"task": "Original"},
            "bounds": h.policy,
            "model_id": h.model.id,
            "created_at": now,
            "deadline_at": now + timedelta(hours=1),
            **changes,
        }
    )


def step_record(h, run, **changes):
    return NativeExecutionStep(
        **{
            "workspace_id": h.workspace,
            "project_id": h.first.id,
            "run_id": run.id,
            "root_run_id": run.root_run_id,
            "sequence": 1,
            "fence": 1,
            "operation_id": uuid4(),
            "kind": "model",
            "request_digest": "b" * 64,
            "request": {"prompt": "Private bounded input"},
            **changes,
        }
    )


def test_execution_policy_and_workflow_versions_scope_and_detachment(execution):
    h = execution
    assert h.store.execution_policy(h.workspace, h.first.id) == h.policy
    assert h.store.execution_policy(h.workspace, h.second.id) is None
    assert h.store.execution_policy(h.foreign_workspace, h.first.id) is None
    h.store.save_execution_policy(h.policy.model_copy(update={"version": 2, "enabled": False}), 1)
    with pytest.raises(InvalidTransitionError):
        h.store.save_execution_policy(h.policy, 0)
    for changes in ({"project_id": h.foreign.id}, {"issued_by": h.outsider}, {"version": 3}):
        with pytest.raises(InvalidTransitionError):
            h.store.save_execution_policy(
                h.policy.model_copy(update={"project_id": h.second.id, **changes}), 0
            )
    config = NativeTaskWorkflow(
        workspace_id=h.workspace,
        project_id=h.first.id,
        task_id=h.task.id,
        version=1,
        dependency_ids=(h.other.id,),
    )
    h.store.save_task_workflow(config, 0)
    assert h.store.task_workflow(h.workspace, h.first.id, h.task.id) == config
    assert h.store.task_workflows(h.workspace, h.first.id) == (config,)
    assert h.store.task_workflows(h.workspace, h.second.id) == ()
    for changes in (
        {"dependency_ids": (h.task.id,)},
        {"dependency_ids": (uuid4(),)},
        {"project_id": h.second.id},
        {"version": 4},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.save_task_workflow(config.model_copy(update={"version": 2, **changes}), 1)
    updated = config.model_copy(update={"version": 2, "dependency_ids": ()})
    h.store.save_task_workflow(updated, 1)
    assert h.store.task_workflow(h.workspace, h.first.id, h.task.id) == updated


def test_execution_run_scope_live_uniqueness_and_concurrent_admission(execution):
    h = execution
    candidates = (run_record(h), run_record(h))

    def admit(value):
        try:
            h.store.insert_execution_run(value)
            return value
        except InvalidTransitionError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [value for value in executor.map(admit, candidates) if value]
    assert len(winners) == 1
    run = winners[0]
    assert h.store.execution_active_runs(h.workspace, h.first.id) == (run,)
    assert h.store.execution_task_runs(h.workspace, h.first.id, h.task.id) == (run,)
    assert h.store.execution_runs(h.workspace, h.first.id, 1) == ()
    assert h.store.execution_run(h.workspace, h.second.id, run.id) is None
    assert h.store.execution_run(h.foreign_workspace, h.first.id, run.id) is None
    detached = h.store.execution_run(h.workspace, h.first.id, run.id)
    detached.context["task"] = "mutated copy"
    assert h.store.execution_run(h.workspace, h.first.id, run.id).context["task"] == "Original"
    assert (h.workspace, h.first.id) in h.store.execution_project_scopes()


@pytest.mark.parametrize("field", ["task_id", "agent_id", "model_id", "runner_id", "schedule_id"])
def test_execution_run_rejects_foreign_references(execution, field):
    h = execution
    changes = {field: uuid4()}
    if field == "schedule_id":
        changes["schedule_definition_version"] = 1
    with pytest.raises(InvalidTransitionError):
        h.store.insert_execution_run(run_record(h, **changes))


def test_execution_immutable_input_progress_cas_and_retry(execution):
    h = execution
    run = run_record(h)
    h.store.insert_execution_run(run)
    for changes in (
        {"context": {"task": "changed"}},
        {"task_version": 2},
        {"model_id": uuid4()},
        {"version": 3},
        {"issued_by": h.member},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.update_execution_run(run.model_copy(update={"version": 2, **changes}), 1)
    running = run.model_copy(update={"version": 2, "status": "running", "fence": 1, "attempt": 1})
    h.store.update_execution_run(running, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.update_execution_run(running, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.update_execution_run(running.model_copy(update={"version": 3, "fence": 0}), 2)
    failed = running.model_copy(update={"version": 3, "status": "failed"})
    h.store.update_execution_run(failed, 2)
    queued = failed.model_copy(update={"version": 4, "status": "queued", "fence": 2})
    h.store.update_execution_run(queued, 3)
    completed = queued.model_copy(
        update={"version": 5, "status": "completed", "result_text": "Draft"}
    )
    h.store.update_execution_run(completed, 4)
    with pytest.raises(InvalidTransitionError):
        h.store.update_execution_run(
            completed.model_copy(update={"version": 6, "status": "queued"}), 5
        )
    assert h.store.execution_active_runs(h.workspace, h.first.id) == ()


def test_execution_children_share_root_and_validate_parent(execution):
    h = execution
    root = run_record(h)
    h.store.insert_execution_run(root)
    child = run_record(
        h,
        task_id=h.other.id,
        root_run_id=root.id,
        parent_run_id=root.id,
        depth=1,
        deadline_at=root.deadline_at,
    )
    h.store.insert_execution_run(child)
    assert h.store.execution_root_runs(h.workspace, h.first.id, root.id) == (root, child)
    assert h.store.execution_root_runs(h.workspace, h.second.id, root.id) == ()
    for changes in ({"parent_run_id": uuid4()}, {"root_run_id": uuid4()}, {"depth": 2}):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_execution_run(child.model_copy(update={"id": uuid4(), **changes}))


def test_execution_child_cannot_enlarge_parent_resource_authority(execution):
    h = execution
    root = run_record(h)
    h.store.insert_execution_run(root)
    child = run_record(
        h,
        task_id=h.other.id,
        root_run_id=root.id,
        parent_run_id=root.id,
        depth=1,
        deadline_at=root.deadline_at,
    )
    for field, enlarged in (("max_steps", 9), ("max_cost_microusd", 1), ("max_children", 9)):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_execution_run(
                child.model_copy(
                    update={
                        "bounds": h.policy.model_copy(update={field: enlarged}),
                    }
                )
            )
    for changes in (
        {"deadline_at": root.deadline_at + timedelta(seconds=1)},
        {"issued_by": h.member, "bounds": h.policy.model_copy(update={"issued_by": h.member})},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_execution_run(child.model_copy(update=changes))
    narrowed = child.model_copy(update={"bounds": h.policy.model_copy(update={"max_steps": 4})})
    h.store.insert_execution_run(narrowed)
    assert h.store.execution_run(h.workspace, h.first.id, child.id) == narrowed


def test_steps_keep_immutable_call_identity_and_outcomes(execution):
    h = execution
    run = run_record(h)
    h.store.insert_execution_run(run)
    step = step_record(h, run)
    h.store.insert_execution_step(step)
    for changes in (
        {},
        {"id": uuid4()},
        {"id": uuid4(), "sequence": 2},
        {"id": uuid4(), "run_id": uuid4()},
        {"id": uuid4(), "root_run_id": uuid4()},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_execution_step(step.model_copy(update=changes))
    assert h.store.execution_steps(h.workspace, h.first.id, run.id) == (step,)
    assert h.store.execution_root_steps(h.workspace, h.first.id, run.id) == (step,)
    assert h.store.execution_step(h.workspace, h.second.id, step.id) is None
    for changes in ({"request": {"prompt": "changed"}}, {"fence": 2}, {"usage_id": uuid4()}):
        with pytest.raises(InvalidTransitionError):
            h.store.update_execution_step(step.model_copy(update={"version": 2, **changes}), 1)
    sent = step.model_copy(update={"version": 2, "status": "dispatched"})
    h.store.update_execution_step(sent, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.update_execution_step(
            sent.model_copy(update={"version": 3, "status": "prepared"}), 2
        )
    unknown = sent.model_copy(update={"version": 3, "status": "unknown"})
    h.store.update_execution_step(unknown, 2)
    completed = unknown.model_copy(
        update={"version": 4, "status": "completed", "result": {"output": "Known response"}}
    )
    h.store.update_execution_step(completed, 3)
    with pytest.raises(InvalidTransitionError):
        h.store.update_execution_step(completed.model_copy(update={"version": 5, "result": {}}), 4)
    detached = h.store.execution_step(h.workspace, h.first.id, step.id)
    detached.result["output"] = "tampered"
    assert h.store.execution_step(h.workspace, h.first.id, step.id) == completed


def test_events_signals_and_waits_are_durable_unique_and_scoped(execution):
    h = execution
    run = run_record(h)
    h.store.insert_execution_run(run)
    events = [
        NativeExecutionEvent(
            workspace_id=h.workspace,
            project_id=h.first.id,
            run_id=run.id,
            sequence=index,
            kind="checkpoint",
            details={"index": index},
        )
        for index in (1, 2)
    ]
    for event in events:
        h.store.append_execution_event(event)
    assert h.store.execution_events(h.workspace, h.first.id, run.id, 1, 1) == (events[1],)
    for changes in (
        {},
        {"id": uuid4()},
        {"id": uuid4(), "run_id": uuid4()},
        {"id": uuid4(), "project_id": h.second.id},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.append_execution_event(events[0].model_copy(update=changes))
    correlation = uuid4()
    signal = NativeExecutionSignal(
        workspace_id=h.workspace,
        project_id=h.first.id,
        run_id=run.id,
        correlation_id=correlation,
        received_by=h.owner,
        text="Proceed",
    )
    h.store.insert_execution_signal(signal)
    assert h.store.execution_signal(h.workspace, h.first.id, run.id, correlation) == signal
    assert h.store.execution_signal(h.workspace, h.second.id, run.id, correlation) is None
    for changes in (
        {"id": uuid4()},
        {"id": uuid4(), "correlation_id": uuid4(), "received_by": h.outsider},
    ):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_execution_signal(signal.model_copy(update=changes))
    wait = NativeExecutionWait(
        workspace_id=h.workspace,
        project_id=h.first.id,
        run_id=run.id,
        correlation_id=correlation,
        kind="human",
        question="Proceed?",
        deadline_at=utc_now() + timedelta(hours=1),
    )
    h.store.insert_execution_wait(wait)
    for changes in ({"id": uuid4()}, {"id": uuid4(), "correlation_id": uuid4()}):
        with pytest.raises(InvalidTransitionError):
            h.store.insert_execution_wait(wait.model_copy(update=changes))
    with pytest.raises(InvalidTransitionError):
        h.store.update_execution_wait(
            wait.model_copy(update={"version": 2, "question": "changed"}), 1
        )
    resolved = wait.model_copy(
        update={"version": 2, "status": "resolved", "response": "Proceed", "resolved_by": h.owner}
    )
    h.store.update_execution_wait(resolved, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.update_execution_wait(
            resolved.model_copy(update={"version": 3, "status": "pending"}), 2
        )
    assert h.store.execution_waits(h.workspace, h.first.id, run.id) == (resolved,)


def test_schedule_occurrences_revisions_and_runner_revocation(execution):
    h = execution
    now = utc_now()
    schedule = NativeExecutionSchedule(
        workspace_id=h.workspace,
        project_id=h.first.id,
        task_id=h.task.id,
        issued_by=h.owner,
        next_run_at=now,
    )
    h.store.insert_execution_schedule(schedule)
    with pytest.raises(InvalidTransitionError):
        h.store.insert_execution_schedule(schedule.model_copy(update={"id": uuid4()}))
    for changes in ({"task_id": h.other.id}, {"last_run_id": uuid4()}, {"version": 3}):
        with pytest.raises(InvalidTransitionError):
            h.store.update_execution_schedule(
                schedule.model_copy(update={"version": 2, **changes}), 1
            )
    run = run_record(h, schedule_id=schedule.id, schedule_definition_version=1)
    h.store.insert_execution_run(run)
    advanced = schedule.model_copy(
        update={"version": 2, "next_run_at": None, "occurrence_count": 1, "last_run_id": run.id}
    )
    h.store.update_execution_schedule(advanced, 1)
    assert h.store.execution_schedule(h.workspace, h.first.id, schedule.id) == advanced
    assert h.store.execution_schedules(h.workspace, h.first.id) == (advanced,)
    assert h.store.execution_schedules(h.workspace, h.second.id) == ()
    with pytest.raises(InvalidTransitionError):
        h.store.update_execution_schedule(
            advanced.model_copy(update={"version": 3, "occurrence_count": 0}), 2
        )
    runner = NativeExecutionRunner(
        workspace_id=h.workspace,
        project_id=h.first.id,
        issued_by=h.owner,
        name="Local",
        token_hash="a" * 64,
        expires_at=now + timedelta(days=1),
    )
    h.store.insert_execution_runner(runner)
    assert h.store.execution_runner_by_hash(runner.token_hash) == runner
    assert h.store.execution_runner_by_hash("b" * 64) is None
    assert h.store.execution_runners(h.workspace, h.first.id) == (runner,)
    assert h.store.execution_runner(h.workspace, h.second.id, runner.id) is None
    with pytest.raises(InvalidTransitionError):
        h.store.insert_execution_runner(runner.model_copy(update={"id": uuid4()}))
    revoked = runner.model_copy(update={"version": 2, "status": "revoked"})
    h.store.update_execution_runner(revoked, 1)
    with pytest.raises(InvalidTransitionError):
        h.store.update_execution_runner(
            revoked.model_copy(update={"version": 3, "status": "active"}), 2
        )


def test_execution_records_rollback_with_parent_transaction(execution):
    h = execution
    run = run_record(h)
    with pytest.raises(RuntimeError), h.store.transaction(h.workspace):
        h.store.insert_execution_run(run)
        h.store.insert_execution_step(step_record(h, run))
        raise RuntimeError("rollback")
    assert h.store.execution_run(h.workspace, h.first.id, run.id) is None
    assert h.store.execution_steps(h.workspace, h.first.id, run.id) == ()


def test_late_checkpoint_survives_issuer_membership_loss(execution):
    h = execution
    run = run_record(h)
    h.store.insert_execution_run(run)
    sent = step_record(h, run, status="dispatched")
    h.store.insert_execution_step(sent)
    h.store.delete_membership(h.owner, h.workspace)
    known = sent.model_copy(
        update={"version": 2, "status": "completed", "result": {"draft": "Known"}}
    )
    h.store.update_execution_step(known, 1)
    assert h.store.execution_step(h.workspace, h.first.id, sent.id) == known
