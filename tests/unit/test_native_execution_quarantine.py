"""Restored authority and late effects cannot cross the installation recovery fence."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest

from simon.domain.errors import RecoveryQuarantinedError
from simon.domain.models import utc_now
from simon.domain.native_execution import (
    CreateExecutionSchedule,
    EnrollExecutionRunner,
    ExecutionCommand,
    UpdateExecutionPolicy,
    UpdateExecutionSchedule,
)
from simon.domain.native_projects import NativeCommand, VersionedNativeCommand
from simon.services.native_execution import NativeExecutionService
from simon.services.recovery import (
    RecoveryEvidence,
    RecoveryGuard,
    begin_recovery,
    complete_recovery,
)
from tests.unit.test_native_execution import (
    advance,
    claim,
    command_key,
    draft,
    new_task,
    setup_execution,
    start,
)


def evidence(backup_id):
    return RecoveryEvidence(
        operator="Synthetic operator",
        backup_id=backup_id,
        database_verification="Synthetic matching database verified",
        files_verification="Synthetic matching files verified",
        keys_verification="Synthetic matching keys verified",
        provider_reconciliation="Synthetic provider effects reconciled",
        liability_reconciliation="Synthetic liabilities reconciled",
        inflight_termination="Synthetic old writers stopped and verified",
        unresolved_effects=[],
        unresolved_liabilities=[],
    )


def begin_restore(h):
    h.restore = begin_recovery(
        h.witness,
        operator="Synthetic operator",
        reason="Synthetic supported restore failure drill",
        backup_id="synthetic-backup",
        writers_stopped=True,
    )


def complete_restore(h):
    complete_recovery(h.witness, h.restore.epoch, evidence(h.restore.backup_id))
    h.recovery = RecoveryGuard(h.witness)
    h.execution.recovery = h.models.usage.recovery = h.recovery


def guarded_execution(tmp_path):
    h = setup_execution()
    h.witness = tmp_path / "outside-backup" / "recovery.json"
    begin_restore(h)
    complete_restore(h)
    reissue_policy(h)
    h.runner = new_runner(h)
    return h


def schedule(h):
    task = new_task(h)
    return h.execution.create_schedule(
        h.owner_actor,
        h.first.id,
        task.id,
        CreateExecutionSchedule(
            expected_task_version=task.version,
            next_run_at=utc_now() - timedelta(seconds=60),
            max_occurrences=1,
            **command_key(),
        ),
    )


def reissue_policy(h):
    current = h.store.execution_policy(h.workspace, h.first.id)
    return h.execution.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateExecutionPolicy(
            expected_version=current.version,
            enabled=True,
            **command_key(),
        ),
    )


def new_runner(h):
    enrolled = h.execution.enroll_runner(
        h.owner_actor,
        h.first.id,
        EnrollExecutionRunner(name="Reenrolled worker", **command_key()),
    )
    return h.execution.runner(enrolled["token"])


def test_quarantined_claim_and_status_read_preserve_pending_work_and_due_schedule(tmp_path):
    h = guarded_execution(tmp_path)
    run = start(h)
    due = schedule(h)
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    begin_restore(h)

    with pytest.raises(RecoveryQuarantinedError):
        claim(h)
    h.execution._tick(h.workspace, h.first.id)
    view = h.execution.view(h.owner_actor, h.first.id)

    current = h.store.execution_run(h.workspace, h.first.id, UUID(run["id"]))
    assert current.status == "queued" and current.version == run["version"]
    assert h.store.execution_schedule(h.workspace, h.first.id, due.id) == due
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks
    assert not h.calls
    assert all(not item["eligible"] for item in view["candidates"])


def test_old_runner_and_grant_remain_fenced_after_recovery_becomes_active(tmp_path):
    h = guarded_execution(tmp_path)
    original_runner = h.runner
    begin_restore(h)
    complete_restore(h)

    with pytest.raises(RecoveryQuarantinedError, match="predates"):
        claim(h)
    with pytest.raises(RecoveryQuarantinedError, match="predates"):
        start(h)

    reissue_policy(h)
    h.runner = new_runner(h)
    run = start(h)
    lease = claim(h)
    assert lease["run_id"] == run["id"]
    with pytest.raises(RecoveryQuarantinedError, match="predates"):
        h.execution.claim(original_runner, NativeCommand(**command_key()))
    assert advance(h, lease, draft())["status"] == "completed"
    assert len(h.calls) == 1


def test_actual_store_rewind_cannot_redispatch_completed_run_with_old_runner(tmp_path):
    h = guarded_execution(tmp_path)
    run = start(h)
    # The witness and provider are deliberately outside the restored database.
    snapshot = deepcopy({key: value for key, value in vars(h.store).items() if key != "_lock"})
    assert advance(h, claim(h), draft())["status"] == "completed"
    assert len(h.calls) == 1

    begin_restore(h)
    vars(h.store).update(deepcopy(snapshot))
    restored = h.store.execution_run(h.workspace, h.first.id, UUID(run["id"]))
    assert restored.status == "queued"
    with pytest.raises(RecoveryQuarantinedError):
        claim(h)

    complete_restore(h)
    h.execution = NativeExecutionService(h.store, h.projects, h.models)
    with pytest.raises(RecoveryQuarantinedError, match="predates"):
        claim(h)
    h.execution.view(h.owner_actor, h.first.id)
    assert h.store.execution_run(h.workspace, h.first.id, restored.id) == restored
    assert h.store.workspace_exists(h.workspace)
    assert len(h.calls) == 1


def test_old_schedule_requires_explicit_reissue_even_with_fresh_grant_and_runner(tmp_path):
    h = guarded_execution(tmp_path)
    due = schedule(h)
    tasks = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    begin_restore(h)
    complete_restore(h)
    reissue_policy(h)
    h.runner = new_runner(h)

    assert claim(h) is None
    assert h.store.execution_schedule(h.workspace, h.first.id, due.id) == due
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks

    reissued = h.execution.update_schedule(
        h.owner_actor,
        h.first.id,
        due.id,
        UpdateExecutionSchedule(
            expected_version=due.version,
            enabled=True,
            next_run_at=due.next_run_at,
            max_occurrences=1,
            **command_key(),
        ),
    )
    assert reissued.recovery_epoch == h.recovery.epoch
    lease = claim(h)
    run = h.store.execution_run(h.workspace, h.first.id, UUID(lease["run_id"]))
    assert run.schedule_id == due.id
    assert h.store.execution_schedule(h.workspace, h.first.id, due.id).occurrence_count == 1


def test_active_recovery_does_not_reclassify_pre_restore_pending_run(tmp_path):
    h = guarded_execution(tmp_path)
    run = start(h)
    before = h.store.execution_run(h.workspace, h.first.id, UUID(run["id"]))
    begin_restore(h)
    complete_restore(h)
    reissue_policy(h)

    h.execution.view(h.owner_actor, h.first.id)
    assert h.store.execution_run(h.workspace, h.first.id, before.id) == before
    assert not h.calls


@pytest.mark.parametrize("reactivate", [False, True])
def test_model_return_after_restore_retains_uncertain_slot_without_publishing(tmp_path, reactivate):
    h = guarded_execution(tmp_path)
    run = start(h)
    lease = claim(h)

    def restore_during_call(request):
        begin_restore(h)
        if reactivate:
            complete_restore(h)

    h.hook = restore_during_call
    with pytest.raises(RecoveryQuarantinedError):
        advance(h, lease, draft())
    (step,) = h.store.execution_steps(h.workspace, h.first.id, UUID(run["id"]))
    usage = h.store.model_usage(h.workspace, h.first.id, step.usage_id)
    assert step.status == "dispatched" and step.result == {}
    assert usage.status == "dispatched" and usage.recovery_epoch != str(h.restore.epoch)
    assert h.store.model_usage_totals(h.workspace, None, utc_now()).active_calls == 1
    assert h.store.artifacts(h.workspace, h.first.id) == ()
    assert len(h.calls) == 1


def test_quarantine_after_known_cost_settlement_withholds_result_checkpoint(tmp_path, monkeypatch):
    h = guarded_execution(tmp_path)
    run = start(h)
    lease = claim(h)
    settle = h.models.usage.settle

    def settle_then_quarantine(*args, **kwargs):
        settled = settle(*args, **kwargs)
        begin_restore(h)
        return settled

    monkeypatch.setattr(h.models.usage, "settle", settle_then_quarantine)
    with pytest.raises(RecoveryQuarantinedError):
        advance(h, lease, draft())

    (step,) = h.store.execution_steps(h.workspace, h.first.id, UUID(run["id"]))
    usage = h.store.model_usage(h.workspace, h.first.id, step.usage_id)
    current = h.store.execution_run(h.workspace, h.first.id, UUID(run["id"]))
    assert usage.status == "settled"
    assert step.status == "dispatched" and step.result == {}
    assert current.result_text == "" and current.status == "running"
    assert h.store.artifacts(h.workspace, h.first.id) == ()
    assert len(h.calls) == 1


def test_quarantine_before_reference_effect_leaves_receipt_absent(tmp_path, monkeypatch):
    h = guarded_execution(tmp_path)
    run = start(h)
    lease = claim(h)
    advance(
        h,
        lease,
        {"kind": "reference", "summary": "Record an echo.", "operation": "echo", "text": "safe"},
    )
    steps = h.store.execution_steps(h.workspace, h.first.id, UUID(run["id"]))
    reference = next(step for step in steps if step.kind == "reference")
    assert reference.status == "prepared"
    operation = h.recovery.operation
    boundaries = 0

    @contextmanager
    def quarantine_at_effect():
        nonlocal boundaries
        boundaries += 1
        if boundaries == 2:
            begin_restore(h)
        with operation() as epoch:
            yield epoch

    monkeypatch.setattr(h.recovery, "operation", quarantine_at_effect)
    with pytest.raises(RecoveryQuarantinedError):
        advance(h, lease)
    assert h.execution.reference.lookup(h.workspace, h.first.id, reference.operation_id) is None
    current = h.store.execution_step(h.workspace, h.first.id, reference.id)
    assert current.status == "dispatched" and current.result == {}
    assert len(h.calls) == 1


def test_completed_checkpoint_cannot_apply_in_quarantine(tmp_path, monkeypatch):
    h = guarded_execution(tmp_path)
    run = start(h)
    lease = claim(h)
    apply_checkpoint = h.execution._apply_checkpoint

    def crash(*args):
        raise RuntimeError("Synthetic crash before checkpoint application")

    monkeypatch.setattr(h.execution, "_apply_checkpoint", crash)
    with pytest.raises(RuntimeError, match="Synthetic crash"):
        advance(h, lease, draft())
    monkeypatch.setattr(h.execution, "_apply_checkpoint", apply_checkpoint)
    current = h.store.execution_run(h.workspace, h.first.id, UUID(run["id"]))
    (step,) = h.store.execution_steps(h.workspace, h.first.id, current.id)
    assert step.status == "completed"
    begin_restore(h)

    with pytest.raises(RecoveryQuarantinedError):
        h.execution._advance_checkpoint(current, step)
    assert h.store.execution_run(h.workspace, h.first.id, current.id) == current
    assert h.store.artifacts(h.workspace, h.first.id) == ()
    assert len(h.calls) == 1


def test_quarantine_allows_authorized_revocation_and_disabling_old_schedule_and_grant(tmp_path):
    h = guarded_execution(tmp_path)
    due = schedule(h)
    run = start(h)
    begin_restore(h)
    h.execution.revoke_runner(
        h.owner_actor,
        h.first.id,
        h.runner.id,
        VersionedNativeCommand(expected_version=h.runner.version, **command_key()),
    )
    policy = h.store.execution_policy(h.workspace, h.first.id)
    disabled = h.execution.update_policy(
        h.owner_actor,
        h.first.id,
        UpdateExecutionPolicy(expected_version=policy.version, enabled=False, **command_key()),
    )
    disabled_schedule = h.execution.update_schedule(
        h.owner_actor,
        h.first.id,
        due.id,
        UpdateExecutionSchedule(
            expected_version=due.version,
            enabled=False,
            next_run_at=due.next_run_at,
            max_occurrences=1,
            **command_key(),
        ),
    )
    assert not disabled.enabled and not disabled_schedule.enabled
    assert h.store.execution_runner(h.workspace, h.first.id, h.runner.id).status == "revoked"
    cancelled = h.execution.cancel(
        h.owner_actor,
        h.first.id,
        UUID(run["id"]),
        ExecutionCommand(expected_version=run["version"], **command_key()),
    )
    assert cancelled["status"] == "cancelled"
    assert not h.calls
