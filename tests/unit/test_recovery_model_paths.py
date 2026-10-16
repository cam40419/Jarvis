"""Real recovery witnesses fence model calls and late reviewed result publication."""

from datetime import timedelta
from uuid import UUID

import pytest

from simon.domain.errors import RecoveryQuarantinedError
from simon.domain.native_models import ProbeProjectModel
from simon.services.recovery import RecoveryGuard, begin_recovery, complete_recovery
from tests.recovery_helpers import activate, evidence
from tests.unit.test_model_usage import call, reserve
from tests.unit.test_native_artifacts import (
    current_revision,
    detail,
    review,
    setup_artifacts,
    upload,
)
from tests.unit.test_native_intake import (
    analyze,
    command,
    configure,
    mutate_command,
    setup_intake,
)
from tests.unit.test_project_models import check, enroll, setup_models


def witness(h, tmp_path):
    path = tmp_path / "recovery-witness.json"
    initial = activate(path)
    h.models.usage.recovery = RecoveryGuard(path)
    return path, initial


def begin(path):
    return begin_recovery(
        path,
        operator="synthetic-test-operator",
        reason="Restore synthetic state during a mocked model response.",
        backup_id="synthetic-backup",
        writers_stopped=True,
    )


def usage(h):
    return h.store.model_usage_entries(h.workspace, h.first.id)


def expire_old_holds(h, monkeypatch):
    pending = [entry for entry in usage(h) if entry.status in {"reserved", "dispatched"}]
    assert pending and all(entry.held_microusd > 0 for entry in pending)
    future = max(entry.deadline_at for entry in pending) + timedelta(seconds=1)
    with monkeypatch.context() as clock:
        clock.setattr("simon.services.model_usage.utc_now", lambda: future)
        h.models.usage.recover(h.workspace)
    for original in pending:
        current = h.store.model_usage(h.workspace, h.first.id, original.id)
        assert current.status == "unknown"
        assert current.held_microusd == original.held_microusd
        assert current.charged_microusd == original.charged_microusd


@pytest.mark.parametrize("path_kind", ["qualification", "intake", "artifact_review"])
def test_quarantine_blocks_model_paths_before_provider_dispatch(path_kind, tmp_path):
    if path_kind == "qualification":
        h = setup_models(paid=True)
        model = enroll(h)

        def invoke():
            return check(h, model)

    elif path_kind == "intake":
        h = setup_intake(tmp_path)

        def invoke():
            return analyze(h)

    else:
        h = setup_artifacts(paid=True)
        artifact, _ = upload(h)

        def invoke():
            return review(h, artifact)

    path, _ = witness(h, tmp_path)
    ledger_before = usage(h)
    tasks_before = h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    begin(path)

    with pytest.raises(RecoveryQuarantinedError):
        invoke()

    assert not h.calls
    assert usage(h) == ledger_before
    assert h.store.native_tasks(h.workspace, h.first.id, 0, 100) == tasks_before
    assert not h.store.native_intake_runs(h.workspace, h.first.id)
    if path_kind == "qualification":
        current = h.store.project_model(h.workspace, h.first.id, UUID(model["id"]))
        assert current.qualification_status == "unverified"
    elif path_kind == "artifact_review":
        assert not detail(h, artifact)["reviews"]


def test_recovery_during_probe_preserves_liability_without_qualifying_model(tmp_path, monkeypatch):
    h = setup_models(paid=True)
    model = enroll(h)
    path, initial = witness(h, tmp_path)
    rotations = []
    h.hook = lambda _: rotations.append(begin(path))
    request = ProbeProjectModel(expected_version=model["version"], idempotency_key="old-probe")

    with pytest.raises(RecoveryQuarantinedError):
        h.models.probe(h.owner_actor, h.first.id, UUID(model["id"]), request)

    assert len(h.calls) == 1 and len(rotations) == 1
    current = h.store.project_model(h.workspace, h.first.id, UUID(model["id"]))
    assert current.version == model["version"]
    assert current.qualification_status == "unverified"
    assert current.qualification_usage_id is None
    (entry,) = usage(h)
    assert entry.status == "dispatched" and entry.held_microusd > 0
    assert entry.recovery_epoch == str(initial.epoch) and entry.charged_microusd == 0
    expire_old_holds(h, monkeypatch)

    complete_recovery(path, rotations[0].epoch, evidence())
    h.hook = None
    with pytest.raises(RecoveryQuarantinedError):
        check(h, model)
    assert len(h.calls) == 1
    h.models.usage.recovery = RecoveryGuard(path)
    replay = h.models.probe(h.owner_actor, h.first.id, UUID(model["id"]), request)
    assert not replay["ready"] and len(h.calls) == 1
    fresh = check(h, model)
    assert fresh["ready"] and len(h.calls) == 2
    old = h.store.model_usage(h.workspace, h.first.id, entry.id)
    assert old.status == "unknown" and old.held_microusd == entry.held_microusd
    new = next(item for item in usage(h) if item.id != entry.id)
    assert new.status == "settled" and new.recovery_epoch == str(rotations[0].epoch)


@pytest.mark.parametrize("response_number", [1, 2])
def test_recovery_during_intake_prevents_publication_and_application(
    response_number, tmp_path, monkeypatch
):
    h = setup_intake(tmp_path)
    path, initial = witness(h, tmp_path)
    rotations = []

    def rotate(_, count):
        if count == response_number:
            rotations.append(begin(path))

    h.hook = rotate
    request = command(h)
    with pytest.raises(RecoveryQuarantinedError):
        h.intake.analyze(h.owner_actor, h.first.id, request)

    assert len(h.calls) == response_number and len(rotations) == 1
    (run,) = h.store.native_intake_runs(h.workspace, h.first.id)
    assert run.status == "running"
    assert run.proposal is None and run.review is None
    assert not run.applied_task_ids and not run.applied_agent_ids
    assert not h.store.native_agents(h.workspace, h.first.id, 0, 100)
    assert not h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    entries = {entry.phase: entry for entry in usage(h)}
    assert all(entry.recovery_epoch == str(initial.epoch) for entry in entries.values())
    interrupted = entries["generation" if response_number == 1 else "review"]
    assert interrupted.status == "dispatched" and interrupted.held_microusd > 0
    if response_number == 1:
        assert entries["review"].status == "reserved"
    else:
        assert entries["generation"].status == "settled"
        assert entries["generation"].charged_microusd > 0
    expire_old_holds(h, monkeypatch)
    old_holds = sum(entry.held_microusd for entry in usage(h))
    assert old_holds > 0

    complete_recovery(path, rotations[0].epoch, evidence())
    h.models.usage.recovery = RecoveryGuard(path)
    h.hook = None
    replay = h.intake.analyze(h.owner_actor, h.first.id, request)
    assert replay.proposal is None and replay.review is None
    assert not replay.applied_task_ids and len(h.calls) == response_number
    assert sum(entry.held_microusd for entry in usage(h)) == old_holds
    for entry in entries.values():
        if entry.status in {"reserved", "dispatched"}:
            with pytest.raises(RecoveryQuarantinedError):
                h.models.usage.dispatch(h.owner_actor, h.first.id, entry.id)


def test_recovery_during_artifact_review_cannot_publish_an_approval(tmp_path, monkeypatch):
    h = setup_artifacts(paid=True)
    artifact, _ = upload(h)
    revision = current_revision(detail(h, artifact))
    path, initial = witness(h, tmp_path)
    rotations = []
    h.hook = lambda _: rotations.append(begin(path))

    with pytest.raises(RecoveryQuarantinedError):
        review(h, artifact)

    assert len(h.calls) == 1 and len(rotations) == 1
    (attempt,) = h.store.artifact_reviews(h.workspace, h.first.id, UUID(revision["id"]), 0, 100)
    assert attempt.status == "running" and attempt.verdict is None
    assert not attempt.summary and not attempt.findings
    assert not detail(h, artifact)["decisions"]
    task = h.store.native_task(h.workspace, h.first.id, attempt.review_task_id)
    assert task.status == "in_progress"
    entry = h.store.model_usage(h.workspace, h.first.id, attempt.usage_id)
    assert entry.status == "dispatched" and entry.held_microusd > 0
    assert entry.recovery_epoch == str(initial.epoch) and entry.charged_microusd == 0
    expire_old_holds(h, monkeypatch)
    held = h.store.model_usage(h.workspace, h.first.id, attempt.usage_id)
    assert held.status == "unknown" and held.held_microusd == entry.held_microusd


def test_completed_recovery_rejects_old_reservation_and_allows_fresh_probe(tmp_path):
    h = setup_models(paid=True)
    model = enroll(h)
    h.binding = h.models.resolve(
        h.owner_actor, h.first.id, UUID(model["id"]), require_qualified=False
    )
    path, initial = witness(h, tmp_path)
    (old,) = reserve(h, call(h, phase="qualification"))
    assert old.recovery_epoch == str(initial.epoch) and old.status == "reserved"
    current = begin(path)
    complete_recovery(path, current.epoch, evidence())

    with pytest.raises(RecoveryQuarantinedError):
        check(h, model)
    assert not h.calls
    h.models.usage.recovery = RecoveryGuard(path)
    with pytest.raises(RecoveryQuarantinedError, match="predates"):
        h.models.usage.dispatch(h.owner_actor, h.first.id, old.id)
    with pytest.raises(RecoveryQuarantinedError, match="predates"):
        reserve(h, old)
    preserved = h.models.usage.release(h.workspace, h.first.id, old.id)
    assert preserved == old

    fresh = check(h, model)
    assert fresh["ready"] and len(h.calls) == 1
    assert h.store.model_usage(h.workspace, h.first.id, old.id) == old
    new = next(entry for entry in usage(h) if entry.id != old.id)
    assert new.status == "settled" and new.recovery_epoch == str(current.epoch)


def test_old_reviewed_intake_cannot_apply_after_recovery_but_new_plan_can(tmp_path):
    h = setup_intake(tmp_path)
    configure(h, auto_staff=False)
    path, _ = witness(h, tmp_path)
    old = analyze(h)
    assert old.status == "ready" and len(h.calls) == 2
    assert all(entry.status == "settled" for entry in usage(h))
    current = begin(path)
    complete_recovery(path, current.epoch, evidence())
    h.models.usage.recovery = RecoveryGuard(path)

    with pytest.raises(RecoveryQuarantinedError, match="predates"):
        h.intake.apply(h.owner_actor, h.first.id, old.id, mutate_command(old.version))
    assert h.store.native_intake_run(h.workspace, h.first.id, old.id) == old
    assert not h.store.native_agents(h.workspace, h.first.id, 0, 100)
    assert not h.store.native_tasks(h.workspace, h.first.id, 0, 100)
    assert len(h.calls) == 2

    fresh = analyze(h)
    applied = h.intake.apply(h.owner_actor, h.first.id, fresh.id, mutate_command(fresh.version))
    assert applied.status == "applied" and len(h.calls) == 4
    assert applied.applied_task_ids and applied.applied_agent_ids
    assert fresh.id != old.id
    new_entries = h.store.model_usage_for_operation(h.workspace, h.first.id, fresh.id)
    assert all(entry.recovery_epoch == str(current.epoch) for entry in new_entries)
