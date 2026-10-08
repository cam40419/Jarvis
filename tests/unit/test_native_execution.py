"""Controller graph limits, atomic actions, durable reference receipts and scheduling."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_execution import (
    CreateExecutionSchedule,
    EnrollExecutionRunner,
    ExecutionCommand,
    RunnerLeaseCommand,
    SignalExecution,
    StartExecution,
    UpdateExecutionPolicy,
    UpdateExecutionSchedule,
    UpdateTaskWorkflow,
)
from simon.domain.native_projects import (
    CreateNativeTask,
    NativeCommand,
    TaskAssignment,
    UpdateNativeTask,
)
from simon.services.native_execution import NativeExecutionService
from tests.unit.test_native_teams import create_role
from tests.unit.test_project_models import check, enroll, setup_models


def command_key():
    return {"idempotency_key": str(uuid4())}


def setup_execution(**bounds):
    h = setup_models()
    h.model = check(h, enroll(h))
    h.agent = create_role(h)
    h.execution = NativeExecutionService(h.store, h.projects, h.models)
    h.execution.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateExecutionPolicy(expected_version=0, enabled=True, **bounds, **command_key()),
    )
    credential = h.execution.enroll_runner(
        h.owner_actor,
        h.first.id,
        EnrollExecutionRunner(name="Local test worker", **command_key()),
    )
    h.runner = h.execution.runner(credential["token"])
    h.calls.clear()
    return h


def new_task(h, **changes):
    return h.projects.create_task(
        h.owner_actor,
        h.first.id,
        CreateNativeTask(
            **{
                "title": "Prepare a candidate",
                "description": "Use evidence, record open questions and request review.",
                "assignment": TaskAssignment(kind="agent", agent_id=h.agent.id),
                **changes,
                **command_key(),
            }
        ),
    )


def start(h, task=None):
    task = task or new_task(h)
    return h.execution.start(
        h.owner_actor,
        h.first.id,
        task.id,
        StartExecution(expected_task_version=task.version, **command_key()),
    )


def claim(h):
    return h.execution.claim(h.runner, NativeCommand(**command_key()))


def advance(h, lease, action=None):
    if action is not None:
        h.response = json.dumps(action)
    return h.execution.step(
        h.runner,
        RunnerLeaseCommand(
            run_id=UUID(lease["run_id"]),
            fence=lease["fence"],
            lease_token=lease["lease_token"],
            **command_key(),
        ),
    )


def current(h, run):
    return h.store.execution_run(h.workspace, h.first.id, UUID(run["id"]))


def detail(h, run):
    return h.execution.detail(h.owner_actor, h.first.id, UUID(run["id"]))


def draft(text="Candidate with evidence and open questions."):
    return {"kind": "draft", "summary": "Prepared for review.", "output": text}


def delegation(h, *agent_ids):
    return {
        "kind": "delegate",
        "summary": "Split independent research responsibilities.",
        "children": [
            {
                "title": f"Research area {index}",
                "description": "Collect evidence and unresolved questions.",
                "agent_id": str(identifier),
                "rationale": "This role owns the relevant expertise.",
            }
            for index, identifier in enumerate(agent_ids or (h.agent.id,))
        ],
    }


def retry(h, run):
    value = current(h, run)
    return h.execution.retry(
        h.owner_actor,
        h.first.id,
        value.id,
        ExecutionCommand(expected_version=value.version, **command_key()),
    )


def clock_forward(monkeypatch, seconds):
    moment = utc_now() + timedelta(seconds=seconds)
    monkeypatch.setattr("simon.services.native_execution.utc_now", lambda: moment)
    monkeypatch.setattr("simon.services.native_workflows.utc_now", lambda: moment)
    monkeypatch.setattr("simon.services.model_usage.utc_now", lambda: moment)
    return moment


def test_delegation_completes_children_then_parent_under_one_budget():
    h = setup_execution()
    root = start(h)
    advance(h, claim(h), delegation(h, h.agent.id, h.agent.id))
    for index in range(2):
        leased = claim(h)
        assert leased["run_id"] != root["id"]
        assert advance(h, leased, draft(f"Child evidence {index}"))["status"] == "completed"
    assert detail(h, root)["run"]["status"] == "queued"
    lease = claim(h)
    assert lease["run_id"] == root["id"]
    result = advance(h, lease, draft("Combined candidate"))
    assert result["status"] == "completed" and result["attempt"] == 1
    assert detail(h, root)["totals"]["model_calls"] == 4
    request = h.calls[-1].content.decode()
    assert "Child evidence 0" in request and "child_candidate" in request
    assert all(
        h.store.native_task(h.workspace, h.first.id, r.task_id).status == "in_review"
        for r in h.store.execution_root_runs(h.workspace, h.first.id, UUID(root["id"]))
    )


@pytest.mark.parametrize("bounds", [{"max_children": 0}, {"max_depth": 0}, {"max_queued_runs": 1}])
def test_delegation_denied_without_partial_board_writes(bounds):
    h = setup_execution(**bounds)
    root = start(h)
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    result = advance(h, claim(h), delegation(h))
    assert result["status"] == "failed" and result["error_code"] == "checkpoint_action_invalid"
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks
    assert not detail(h, root)["children"]
    assert detail(h, root)["totals"]["model_calls"] == 1


def test_second_invalid_delegate_rolls_back_first_child_and_events():
    h = setup_execution()
    root = start(h)
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    result = advance(h, claim(h), delegation(h, h.agent.id, uuid4()))
    assert result["status"] == "failed"
    assert not detail(h, root)["children"]
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks
    assert not any(e["kind"] == "waiting" for e in detail(h, root)["events"])


def test_failed_child_can_be_retried_while_parent_waits():
    h = setup_execution()
    root = start(h)
    advance(h, claim(h), delegation(h))
    lease = claim(h)
    h.response = "invalid JSON"
    failed = advance(h, lease)
    parent = detail(h, root)["run"]
    assert parent["status"] == "waiting" and parent["error_code"] == "child_requires_attention"
    retry(h, failed)
    assert advance(h, claim(h), draft())["status"] == "completed"
    assert claim(h)["run_id"] == root["id"]


def test_parent_and_child_share_model_call_allowance():
    h = setup_execution(max_model_calls=2)
    root = start(h)
    advance(h, claim(h), delegation(h))
    advance(h, claim(h), draft())
    failed = advance(h, claim(h), draft())
    assert failed["id"] == root["id"] and failed["status"] == "failed"
    assert len(h.calls) == 2 and detail(h, root)["totals"]["model_calls"] == 2


def test_cancelling_parent_cancels_descendants_and_pending_waits():
    h = setup_execution()
    root = start(h)
    advance(h, claim(h), delegation(h))
    value = current(h, root)
    h.execution.cancel(
        h.owner_actor,
        h.first.id,
        value.id,
        ExecutionCommand(expected_version=value.version, **command_key()),
    )
    assert current(h, root).status == "cancelled"
    assert all(c["status"] == "cancelled" for c in detail(h, root)["children"])
    assert all(w["status"] == "cancelled" for w in detail(h, root)["waits"])
    assert claim(h) is None


@pytest.mark.parametrize("operation", ["echo", "commit"])
def test_reference_receipt_is_durable_and_included_in_next_model_context(operation):
    h = setup_execution()
    root = start(h)
    lease = claim(h)
    result = advance(
        h,
        lease,
        {
            "kind": "reference",
            "summary": "Exercise receipt.",
            "operation": operation,
            "text": "receipt evidence",
        },
    )
    assert result["status"] == "running" and result["step_number"] == 2
    result = advance(h, lease)
    assert result["status"] == "running" and result["applied_step"] == 2
    assert len(h.calls) == 1
    assert advance(h, lease, draft())["status"] == "completed"
    assert "receipt evidence" in h.calls[-1].content.decode()
    assert detail(h, root)["steps"][1]["result"]["operation"] == operation


def test_unknown_reference_recovers_its_committed_receipt_without_redispatch(monkeypatch):
    h = setup_execution()
    root = start(h)
    lease = claim(h)
    advance(
        h,
        lease,
        {
            "kind": "reference",
            "summary": "Store fixture.",
            "operation": "commit",
            "text": "committed once",
        },
    )
    execute = h.execution.reference.execute
    operations = []

    def lost_response(*args):
        operations.append(args[2])
        execute(*args)
        raise TimeoutError("receipt response lost")

    monkeypatch.setattr(h.execution.reference, "execute", lost_response)
    assert advance(h, lease)["status"] == "unknown"
    h.execution = NativeExecutionService(h.store, h.projects, h.models)
    retry(h, root)
    lease = claim(h)
    assert advance(h, lease)["applied_step"] == 2
    assert advance(h, lease, draft())["status"] == "completed"
    assert len(operations) == 1 and len(h.calls) == 2


def test_unknown_reference_without_receipt_stays_blocked(monkeypatch):
    h = setup_execution()
    root = start(h)
    lease = claim(h)
    advance(
        h,
        lease,
        {
            "kind": "reference",
            "summary": "Store fixture.",
            "operation": "commit",
            "text": "candidate",
        },
    )

    def interrupted(*args):
        raise TimeoutError("unknown outcome")

    monkeypatch.setattr(h.execution.reference, "execute", interrupted)
    assert advance(h, lease)["status"] == "unknown"
    with pytest.raises(InvalidTransitionError, match="definitive receipt"):
        retry(h, root)
    assert claim(h) is None


@pytest.mark.parametrize("reference", [False, True])
def test_timer_releases_worker_and_resumes_after_restart(monkeypatch, reference):
    h = setup_execution()
    root = start(h)
    lease = claim(h)
    if reference:
        advance(
            h,
            lease,
            {
                "kind": "reference",
                "summary": "Delay fixture.",
                "operation": "delay",
                "delay_seconds": 60,
            },
        )
        waiting = advance(h, lease)
    else:
        waiting = advance(
            h, lease, {"kind": "wait", "summary": "Pause briefly.", "wait_seconds": 60}
        )
    assert waiting["status"] == "waiting" and waiting["runner_id"] is None
    assert claim(h) is None
    clock_forward(monkeypatch, 61)
    h.execution = NativeExecutionService(h.store, h.projects, h.models)
    resumed = claim(h)
    assert resumed["run_id"] == root["id"]
    assert advance(h, resumed, draft())["status"] == "completed"


def test_early_correlated_reply_is_consumed_once_without_blocking():
    h = setup_execution()
    root = start(h)
    correlation = UUID(detail(h, root)["correlation_id"])
    signal = SignalExecution(
        correlation_id=correlation, text="Choose the smaller first release.", **command_key()
    )
    h.execution.signal(h.owner_actor, h.first.id, UUID(root["id"]), signal)
    h.execution.signal(h.owner_actor, h.first.id, UUID(root["id"]), signal)
    result = advance(
        h,
        claim(h),
        {
            "kind": "question",
            "summary": "Need scope decision.",
            "question": "How large should the first release be?",
        },
    )
    assert result["status"] == "queued"
    assert advance(h, claim(h), draft())["status"] == "completed"
    assert "Choose the smaller" in h.calls[-1].content.decode()
    assert len(detail(h, root)["waits"]) == 1


def test_step_limit_stops_free_model_loop_before_another_call():
    h = setup_execution(max_steps=1)
    root = start(h)
    lease = claim(h)
    advance(h, lease, {"kind": "question", "summary": "Need input.", "question": "Choose one?"})
    wait = detail(h, root)["waits"][0]
    h.execution.signal(
        h.owner_actor,
        h.first.id,
        UUID(root["id"]),
        SignalExecution(correlation_id=UUID(wait["correlation_id"]), text="First", **command_key()),
    )
    assert advance(h, claim(h), draft())["error_code"] == "step_limit_reached"
    assert len(h.calls) == 1


def test_reference_action_requires_remaining_step_allowance():
    h = setup_execution(max_steps=1)
    root = start(h)
    result = advance(
        h,
        claim(h),
        {"kind": "reference", "summary": "Receipt.", "operation": "echo", "text": "test"},
    )
    assert result["status"] == "failed" and len(detail(h, root)["steps"]) == 1


def test_schedule_coalesces_missed_occurrences_and_does_not_overlap(monkeypatch):
    h = setup_execution()
    task = new_task(h)
    due = utc_now() - timedelta(seconds=3600)
    schedule = h.execution.create_schedule(
        h.owner_actor,
        h.first.id,
        task.id,
        CreateExecutionSchedule(
            expected_task_version=task.version,
            next_run_at=due,
            interval_seconds=60,
            max_occurrences=2,
            **command_key(),
        ),
    )
    lease = claim(h)
    run = h.store.execution_run(h.workspace, h.first.id, UUID(lease["run_id"]))
    assert run.task_id != task.id and run.schedule_id == schedule.id
    assert h.store.native_task(h.workspace, h.first.id, task.id).status == "todo"
    value = h.store.execution_schedule(h.workspace, h.first.id, schedule.id)
    assert value.occurrence_count == 1 and value.next_run_at > utc_now()
    advance(h, lease, {"kind": "question", "summary": "Review schedule.", "question": "Continue?"})
    clock_forward(monkeypatch, 65)
    assert claim(h) is None
    assert h.store.execution_schedule(h.workspace, h.first.id, schedule.id).occurrence_count == 1


def test_exhausted_schedule_cannot_fire_after_date_edit():
    h = setup_execution()
    task = new_task(h)
    due = utc_now() - timedelta(seconds=10)
    schedule = h.execution.create_schedule(
        h.owner_actor,
        h.first.id,
        task.id,
        CreateExecutionSchedule(
            expected_task_version=task.version, next_run_at=due, max_occurrences=1, **command_key()
        ),
    )
    advance(h, claim(h), draft())
    value = h.store.execution_schedule(h.workspace, h.first.id, schedule.id)
    h.execution.update_schedule(
        h.owner_actor,
        h.first.id,
        schedule.id,
        UpdateExecutionSchedule(
            expected_version=value.version,
            enabled=True,
            next_run_at=due,
            max_occurrences=1,
            **command_key(),
        ),
    )
    assert claim(h) is None
    assert h.store.execution_schedule(h.workspace, h.first.id, schedule.id).occurrence_count == 1


def test_schedule_admission_failure_does_not_leave_orphan_task():
    h = setup_execution(max_queued_runs=1)
    start(h)
    task = new_task(h)
    schedule = h.execution.create_schedule(
        h.owner_actor,
        h.first.id,
        task.id,
        CreateExecutionSchedule(
            expected_task_version=task.version, next_run_at=utc_now(), **command_key()
        ),
    )
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    claim(h)
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks
    assert h.store.execution_schedule(h.workspace, h.first.id, schedule.id).occurrence_count == 0


def test_auto_start_only_assigned_work_and_reopened_changed_input():
    h = setup_execution(auto_start=True)
    new_task(h, assignment=TaskAssignment(kind="pool"))
    task = new_task(h)
    lease = claim(h)
    advance(h, lease, draft())
    assert claim(h) is None
    current_task = h.store.native_task(h.workspace, h.first.id, task.id)
    h.projects.update_task(
        h.owner_actor,
        h.first.id,
        task.id,
        UpdateNativeTask(
            expected_version=current_task.version,
            title="Revised research question",
            description=current_task.description,
            assignment=current_task.assignment,
            status="todo",
            **command_key(),
        ),
    )
    lease = claim(h)
    assert lease is not None and lease["task_title"] == "Revised research question"


def test_not_before_blocks_claim_without_consuming_model_call(monkeypatch):
    h = setup_execution()
    task = new_task(h)
    h.execution.update_workflow(
        h.owner_actor,
        h.first.id,
        task.id,
        UpdateTaskWorkflow(
            expected_version=0, not_before=utc_now() + timedelta(seconds=60), **command_key()
        ),
    )
    start(h, task)
    assert claim(h) is None and not h.calls
    clock_forward(monkeypatch, 61)
    assert advance(h, claim(h), draft())["status"] == "completed"


def test_failed_current_authority_check_rolls_back_model_reservation(monkeypatch):
    h = setup_execution()
    root = start(h)

    def revoked(*args):
        raise InvalidTransitionError("Model grant changed before dispatch.")

    monkeypatch.setattr(h.models, "assert_current", revoked)
    assert advance(h, claim(h), draft())["status"] == "failed"
    assert not h.calls and not detail(h, root)["steps"]
    assert len(h.store.model_usage_entries(h.workspace, h.first.id, 0, 100)) == 1


@pytest.mark.parametrize("child", [False, True])
def test_expired_wait_has_distinct_deadline_failure(monkeypatch, child):
    h = setup_execution(run_timeout_seconds=300)
    root = start(h)
    action = (
        delegation(h)
        if child
        else {"kind": "question", "summary": "Need direction.", "question": "Which direction?"}
    )
    advance(h, claim(h), action)
    clock_forward(monkeypatch, 301)
    result = detail(h, root)
    assert result["run"]["status"] == "failed"
    assert result["run"]["error_code"] == "workflow_deadline_expired"
    assert result["waits"][0]["status"] == "timed_out"
    assert not result["can_retry"] and claim(h) is None


def test_disabled_schedule_history_does_not_starve_a_later_due_schedule():
    h = setup_execution()
    task = new_task(h)
    schedule = h.execution.create_schedule(
        h.owner_actor,
        h.first.id,
        task.id,
        CreateExecutionSchedule(
            expected_task_version=task.version, next_run_at=utc_now(), **command_key()
        ),
    )
    # A full first page of historical schedules must not hide the active entry.
    for _ in range(100):
        h.store.insert_execution_schedule(
            schedule.model_copy(
                update={
                    "id": uuid4(),
                    "enabled": False,
                    "created_at": schedule.created_at - timedelta(days=1),
                }
            )
        )
    assert claim(h) is not None
    assert h.store.execution_schedule(h.workspace, h.first.id, schedule.id).occurrence_count == 1
