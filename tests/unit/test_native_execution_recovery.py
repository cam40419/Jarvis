"""Known costs survive interrupted authority; only current leases publish checkpoints."""

import json
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from simon.domain.errors import InvalidTransitionError
from simon.domain.models import utc_now
from simon.domain.native_execution import (
    EnrollExecutionRunner,
    ExecutionCommand,
    RunnerLeaseCommand,
    StartExecution,
    UpdateExecutionPolicy,
    UpdateTaskWorkflow,
)
from simon.domain.native_models import ReconcileModelUsage
from simon.domain.native_projects import CreateNativeTask, NativeCommand, TaskAssignment
from simon.services.native_execution import NativeExecutionService
from tests.unit.test_native_teams import create_role, task_update
from tests.unit.test_project_models import check, enroll, setup_models, update


def recovery_setup(**bounds):
    h = setup_models(paid=True)
    h.model = check(h, enroll(h))
    h.calls.clear()
    h.execution = NativeExecutionService(h.store, h.projects, h.models)
    h.agent = create_role(h)
    h.execution.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateExecutionPolicy(
            expected_version=0,
            enabled=True,
            max_cost_microusd=500_000,
            idempotency_key="execution-authority",
            **bounds,
        ),
    )
    h.task = new_task(h)
    enrolled = h.execution.enroll_runner(
        h.owner_actor,
        h.first.id,
        EnrollExecutionRunner(
            name="Synthetic worker",
            ttl_seconds=600,
            idempotency_key="enroll-synthetic-runner",
        ),
    )
    h.runner = h.execution.runner(enrolled["token"])
    h.token = enrolled["token"]
    h.response = json.dumps(
        {"kind": "draft", "summary": "Candidate ready", "output": "Draft for review."}
    )
    return h


def new_task(h, title="Create a draft"):
    return h.projects.create_task(
        h.owner_actor,
        h.first.id,
        CreateNativeTask(
            title=title,
            description="Produce a useful candidate, with uncertainties stated.",
            assignment=TaskAssignment(kind="agent", agent_id=h.agent.id),
            idempotency_key=str(uuid4()),
        ),
    )


def start(h, task=None):
    task = task or h.task
    return h.execution.start(
        h.owner_actor,
        h.first.id,
        task.id,
        StartExecution(
            expected_task_version=task.version,
            idempotency_key=str(uuid4()),
        ),
    )


def claim(h):
    return h.execution.claim(h.runner, NativeCommand(idempotency_key=str(uuid4())))


def command(lease, **changes):
    return RunnerLeaseCommand(
        **{
            "run_id": lease["run_id"],
            "fence": lease["fence"],
            "lease_token": lease["lease_token"],
            "idempotency_key": str(uuid4()),
            **changes,
        }
    )


def record(h, run):
    return h.store.execution_run(h.workspace, h.first.id, UUID(run["id"]))


def execution_usage(h, run):
    steps = h.store.execution_steps(h.workspace, h.first.id, UUID(run["id"]))
    return tuple(
        h.store.model_usage(h.workspace, h.first.id, step.usage_id)
        for step in steps
        if step.usage_id
    )


def retry(h, run):
    current = record(h, run)
    return h.execution.retry(
        h.owner_actor,
        h.first.id,
        current.id,
        ExecutionCommand(
            expected_version=current.version,
            idempotency_key=str(uuid4()),
        ),
    )


def test_paid_model_charges_exact_call_and_preserves_review_boundary():
    h = recovery_setup()
    run = start(h)
    result = h.execution.step(h.runner, command(claim(h)))
    assert result["status"] == "completed" and len(h.calls) == 1
    (usage,) = execution_usage(h, run)
    assert usage.status == "settled" and usage.charged_microusd == 120 and usage.held_microusd == 0
    assert h.execution.totals(record(h, run)) == {
        "charged_microusd": 120,
        "held_microusd": 0,
        "model_calls": 1,
    }
    assert h.projects.get_task(h.owner_actor, h.first.id, h.task.id).status == "in_review"


@pytest.mark.parametrize("failure", ["response_loss", "missing_usage"])
def test_uncertain_call_keeps_its_hold_and_cannot_retry_without_reconciliation(failure):
    h = recovery_setup()
    run = start(h)
    if failure == "response_loss":

        def timeout(request):
            raise httpx.ReadTimeout("Synthetic lost response", request=request)

        h.hook = timeout
    else:
        h.tokens = {}
    result = h.execution.step(h.runner, command(claim(h)))
    assert result["status"] == "unknown" and result["result_text"] == ""
    (usage,) = execution_usage(h, run)
    assert usage.status == "unknown" and usage.held_microusd > 0
    with pytest.raises(InvalidTransitionError, match="Reconcile"):
        retry(h, run)
    assert claim(h) is None and len(h.calls) == 1
    assert not h.execution.detail(h.owner_actor, h.first.id, UUID(run["id"]))["can_retry"]


def test_explicit_reconciled_retry_keeps_prior_cost_and_uses_a_new_operation(monkeypatch):
    h = recovery_setup()
    run = start(h)

    def timeout(request):
        raise httpx.ReadTimeout("Synthetic lost response", request=request)

    h.hook = timeout
    assert h.execution.step(h.runner, command(claim(h)))["status"] == "unknown"
    (usage,) = execution_usage(h, run)
    future = usage.deadline_at + timedelta(seconds=1)
    for module in ("model_usage", "native_execution", "native_workflows"):
        monkeypatch.setattr("simon.services." + module + ".utc_now", lambda: future)
    h.usage.reconcile(
        h.owner_actor,
        h.first.id,
        usage.id,
        ReconcileModelUsage(
            expected_version=usage.version,
            charged_microusd=70,
            reason="Synthetic provider accounting resolved the call.",
            evidence="Synthetic statement confirms a total of 70 microusd.",
            idempotency_key="reconcile-first-workflow-call",
        ),
    )
    h.hook = None
    assert retry(h, run)["status"] == "queued"
    assert h.execution.step(h.runner, command(claim(h)))["status"] == "completed"
    usages = execution_usage(h, run)
    assert len(usages) == 2 and usages[0].operation_id != usages[1].operation_id
    assert usages[0].status == "reconciled" and usages[1].status == "settled"
    assert h.execution.totals(record(h, run))["charged_microusd"] == 190


def test_known_malformed_response_retry_preserves_charge_and_calls_limit():
    h = recovery_setup(max_model_calls=2)
    run = start(h)
    h.response = "not valid structured JSON"
    first = h.execution.step(h.runner, command(claim(h)))
    assert first["status"] == "failed"
    assert execution_usage(h, run)[0].charged_microusd == 120
    h.response = json.dumps(
        {"kind": "draft", "summary": "Corrected draft", "output": "Corrected candidate"}
    )
    retry(h, run)
    result = h.execution.step(h.runner, command(claim(h)))
    assert result["status"] == "completed" and len(h.calls) == 2
    assert h.execution.totals(record(h, run))["charged_microusd"] == 240


@pytest.mark.parametrize(
    "authority_change",
    ["cancel", "issuer_removed", "runner_revoked", "runner_expired", "model_changed"],
)
def test_late_known_response_settles_cost_without_publishing_after_authority_change(
    authority_change,
    monkeypatch,
):
    h = recovery_setup()
    run = start(h)
    lease = claim(h)

    def change(request):
        if authority_change == "cancel":
            current = record(h, run)
            h.execution.cancel(
                h.owner_actor,
                h.first.id,
                current.id,
                ExecutionCommand(
                    expected_version=current.version,
                    idempotency_key="cancel-during-model-call",
                ),
            )
        elif authority_change == "issuer_removed":
            h.store.delete_membership(h.owner, h.workspace)
        elif authority_change == "model_changed":
            update(h, h.model, label="Changed enrollment revision")
        else:
            current = h.store.execution_runner(h.workspace, h.first.id, h.runner.id)
            if authority_change == "runner_revoked":
                h.store.update_execution_runner(
                    current.model_copy(
                        update={
                            "version": current.version + 1,
                            "status": "revoked",
                        }
                    ),
                    current.version,
                )
            else:
                # Exercise the real expiry check without rewriting immutable credentials.
                future = current.expires_at + timedelta(seconds=1)
                monkeypatch.setattr("simon.services.native_workflows.utc_now", lambda: future)

    h.hook = change
    result = h.execution.step(h.runner, command(lease))
    assert result["status"] != "completed" and result["result_text"] == ""
    (usage,) = execution_usage(h, run)
    assert usage.status == "settled" and usage.charged_microusd == 120
    assert usage.held_microusd == 0 and len(h.calls) == 1
    task = h.store.native_task(h.workspace, h.first.id, h.task.id)
    assert task.status != "in_review"


def test_expired_lease_reclaims_unsent_work_and_rejects_old_step():
    h = recovery_setup()
    run = start(h)
    old = claim(h)
    current = record(h, run)
    h.store.update_execution_run(
        current.model_copy(
            update={
                "version": current.version + 1,
                "lease_until": utc_now() - timedelta(seconds=1),
            }
        ),
        current.version,
    )
    replacement = claim(h)
    assert replacement["fence"] > old["fence"]
    with pytest.raises(InvalidTransitionError, match="lease"):
        h.execution.step(h.runner, command(old))
    assert h.calls == []
    assert h.execution.step(h.runner, command(replacement))["status"] == "completed"
    assert len(h.calls) == 1


def test_dependency_changed_during_model_call_prevents_stale_candidate_but_keeps_charge():
    h = recovery_setup()
    dependency_run = start(h)
    assert h.execution.step(h.runner, command(claim(h)))["status"] == "completed"
    downstream = new_task(h, "Use the candidate as input")
    h.execution.update_workflow(
        h.owner_actor,
        h.first.id,
        downstream.id,
        UpdateTaskWorkflow(
            expected_version=0,
            dependency_ids=(h.task.id,),
            idempotency_key="dependency-definition",
        ),
    )
    run = start(h, downstream)
    lease = claim(h)

    def change_dependency(request):
        task = h.projects.get_task(h.owner_actor, h.first.id, h.task.id)
        h.projects.update_task(
            h.owner_actor,
            h.first.id,
            task.id,
            task_update(task, description="Corrected authoritative input"),
        )

    h.hook = change_dependency
    result = h.execution.step(h.runner, command(lease))
    assert result["status"] == "failed" and result["result_text"] == ""
    assert execution_usage(h, run)[0].charged_microusd == 120
    assert record(h, dependency_run).status == "completed"
    assert h.projects.get_task(h.owner_actor, h.first.id, downstream.id).status == "in_progress"


def test_completed_checkpoint_resumes_without_new_inference_or_charge(monkeypatch):
    h = recovery_setup()
    run = start(h)
    lease = claim(h)
    original = h.execution._apply_checkpoint

    def crash(*args):
        raise RuntimeError("Synthetic checkpoint transition crash")

    monkeypatch.setattr(h.execution, "_apply_checkpoint", crash)
    with pytest.raises(RuntimeError, match="checkpoint transition crash"):
        h.execution.step(h.runner, command(lease))
    assert record(h, run).applied_step == 0
    assert execution_usage(h, run)[0].charged_microusd == 120
    monkeypatch.setattr(h.execution, "_apply_checkpoint", original)
    replay = h.execution.step(h.runner, command(lease))
    assert replay["status"] == "completed" and len(h.calls) == 1
    assert record(h, run).applied_step == 1
    assert h.execution.totals(record(h, run))["charged_microusd"] == 120


def test_expired_dispatched_worker_settles_late_response_without_automatic_publication():
    h = recovery_setup()
    run = start(h)
    lease = claim(h)

    def expire_and_recover(request):
        current = record(h, run)
        h.store.update_execution_run(
            current.model_copy(
                update={
                    "version": current.version + 1,
                    "lease_until": utc_now() - timedelta(seconds=1),
                }
            ),
            current.version,
        )
        assert claim(h) is None
        assert record(h, run).status == "unknown"

    h.hook = expire_and_recover
    late = h.execution.step(h.runner, command(lease))
    assert late["status"] == "unknown" and late["result_text"] == ""
    assert execution_usage(h, run)[0].charged_microusd == 120
    assert (
        h.store.execution_steps(h.workspace, h.first.id, UUID(run["id"]))[0].status == "completed"
    )
    h.hook = None
    retry(h, run)
    assert h.execution.step(h.runner, command(claim(h)))["status"] == "completed"
    assert len(h.calls) == 1


def test_provider_overrun_records_full_charge_and_stops_candidate_publication():
    h = recovery_setup()
    run = start(h)
    h.tokens = {"prompt_tokens": 900_000, "completion_tokens": 900_000}
    result = h.execution.step(h.runner, command(claim(h)))
    (usage,) = execution_usage(h, run)
    assert usage.charged_microusd == 2_700_000 and usage.held_microusd == 0
    assert usage.error_code == "provider_usage_exceeded_reservation"
    assert result["status"] == "failed" and result["result_text"] == ""
    assert (
        h.execution.totals(record(h, run))["charged_microusd"]
        > h.execution._policy(h.workspace, h.first.id).max_cost_microusd
    )


@pytest.mark.parametrize("workspace", [False, True])
def test_current_resource_pause_blocks_model_dispatch_without_new_usage(workspace):
    h = recovery_setup()
    run = start(h)
    lease = claim(h)
    scope = None if workspace else h.first.id
    policy = h.store.model_resource_policy(h.workspace, scope)
    h.store.save_model_resource_policy(
        policy.model_copy(
            update={
                "version": policy.version + 1,
                "paused": True,
            }
        ),
        policy.version,
    )
    result = h.execution.step(h.runner, command(lease))
    assert result["status"] == "failed" and h.calls == []
    assert execution_usage(h, run) == ()
