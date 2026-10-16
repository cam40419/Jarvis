"""Recovery generations remain immutable usage authority across restore and restart."""

from datetime import timedelta
from uuid import uuid4

import pytest

from simon.domain.errors import InvalidTransitionError, RecoveryQuarantinedError
from simon.domain.models import utc_now
from simon.domain.native_models import ProjectModel
from simon.services.model_usage import ModelUsageService
from simon.services.native_projects import NativeProjectService
from simon.services.recovery import (
    RecoveryEvidence,
    RecoveryGuard,
    begin_recovery,
    complete_recovery,
)
from tests.contract.test_native_model_store import usage
from tests.contract.test_native_project_store import seed_native_projects
from tests.integration.test_native_project_migration import (
    empty_native_schema as empty_native_schema,
)


def model_records(store):
    h = seed_native_projects(store)
    h.model = ProjectModel(
        workspace_id=h.workspace,
        project_id=h.first.id,
        template_id="local",
        label="Synthetic recovery model",
        created_by=h.owner,
    )
    store.insert_project_model(h.model)
    return h


def evidence(backup_id):
    return RecoveryEvidence(
        operator="Synthetic operator",
        backup_id=backup_id,
        database_verification="Synthetic database state verified.",
        files_verification="Synthetic original files verified.",
        keys_verification="Synthetic encryption keys verified.",
        provider_reconciliation="No real provider requests occurred.",
        liability_reconciliation="All synthetic usage obligations retained.",
        inflight_termination="All synthetic obsolete workers terminated.",
        unresolved_effects=[],
        unresolved_liabilities=[],
    )


def active_guard(path, backup_id="synthetic-backup"):
    state = begin_recovery(
        path,
        operator="Synthetic operator",
        reason="Controlled recovery generation fixture",
        backup_id=backup_id,
        writers_stopped=True,
    )
    complete_recovery(path, state.epoch, evidence(backup_id))
    return RecoveryGuard(path)


def expired_usage(h, **changes):
    started = utc_now() - timedelta(minutes=10)
    return usage(h, started_at=started, deadline_at=started + timedelta(minutes=1), **changes)


def test_usage_recovery_epoch_is_scoped_and_immutable_in_store(store):
    h = model_records(store)
    record = usage(h, recovery_epoch=str(uuid4()))
    store.insert_model_usage(record)
    assert (
        store.model_usage(h.workspace, h.first.id, record.id).recovery_epoch
        == record.recovery_epoch
    )
    assert store.model_usage(h.workspace, h.second.id, record.id) is None
    assert store.model_usage(h.foreign_workspace, h.first.id, record.id) is None
    for epoch in (str(uuid4()), None):
        with pytest.raises(InvalidTransitionError):
            store.update_model_usage(
                record.model_copy(update={"version": 2, "recovery_epoch": epoch}),
                1,
            )
    dispatched = record.model_copy(update={"version": 2, "status": "dispatched"})
    store.update_model_usage(dispatched, 1)
    assert store.model_usage(h.workspace, h.first.id, record.id) == dispatched
    assert store.model_usage_for_operation(h.workspace, h.first.id, record.operation_id) == (
        dispatched,
    )


def test_recovery_pending_count_includes_all_tenants_and_zero_fee_calls(store):
    h = model_records(store)
    assert store.recovery_usage_pending() == 0
    reserved = usage(h, reserved_microusd=0, held_microusd=0)
    dispatched = usage(h, status="dispatched")
    unknown = usage(
        h,
        workspace_id=h.foreign_workspace,
        project_id=h.foreign.id,
        requested_by=h.outsider,
        model_id=None,
        model_version=0,
        status="unknown",
        reserved_microusd=0,
        held_microusd=0,
    )
    for value in (
        reserved,
        dispatched,
        unknown,
        usage(h, status="settled", held_microusd=0),
        usage(h, status="released", held_microusd=0),
    ):
        store.insert_model_usage(value)
    assert store.recovery_usage_pending() == 3
    store.delete_membership(h.outsider, h.foreign_workspace)
    assert store.recovery_usage_pending() == 3
    with pytest.raises(RuntimeError), store.transaction(h.workspace):
        store.update_model_usage(
            reserved.model_copy(update={"version": 2, "status": "released"}), 1
        )
        assert store.recovery_usage_pending() == 2
        raise RuntimeError("Injected pending-call resolution failure")
    assert store.recovery_usage_pending() == 3
    store.update_model_usage(reserved.model_copy(update={"version": 2, "status": "released"}), 1)
    store.update_model_usage(
        dispatched.model_copy(
            update={
                "version": 2,
                "status": "settled",
                "held_microusd": 0,
            }
        ),
        1,
    )
    assert store.recovery_usage_pending() == 1


@pytest.mark.parametrize("amount", [0, 100])
def test_old_epoch_expired_reservation_becomes_unknown_preserving_hold_and_call_slot(
    store,
    tmp_path,
    amount,
):
    h = model_records(store)
    witness = tmp_path / "operator-state" / "recovery.json"
    old_guard = active_guard(witness)
    record = expired_usage(
        h,
        recovery_epoch=old_guard.assert_active(),
        reserved_microusd=amount,
        held_microusd=amount,
    )
    store.insert_model_usage(record)
    # A supported restore changes the external generation, independently of DB rewind.
    new_state = begin_recovery(
        witness,
        operator="Synthetic operator",
        reason="Simulated backup restored before dispatch",
        backup_id="restored-synthetic-backup",
        writers_stopped=True,
    )
    current_guard = RecoveryGuard(witness)
    projects = NativeProjectService(store)
    current = ModelUsageService(store, projects, recovery=current_guard)
    stale = ModelUsageService(store, projects, recovery=old_guard)
    store.delete_membership(h.owner, h.workspace)
    with store.transaction(h.workspace):
        current.recover(h.workspace)
    recovered = store.model_usage(h.workspace, h.first.id, record.id)
    assert recovered.status == "unknown"
    assert recovered.recovery_epoch == record.recovery_epoch
    assert recovered.held_microusd == amount and recovered.charged_microusd == 0
    totals = store.model_usage_totals(h.workspace, None, utc_now())
    assert totals.held_microusd == amount and totals.active_calls == 1
    assert stale.release(h.workspace, h.first.id, record.id) == recovered
    assert stale.settle(h.workspace, h.first.id, record.id, None, unknown=True) == recovered
    complete_recovery(witness, new_state.epoch, evidence("restored-synthetic-backup"))
    assert current.release(h.workspace, h.first.id, record.id) == recovered
    assert current.settle(h.workspace, h.first.id, record.id, None, unknown=True) == recovered
    with pytest.raises(RecoveryQuarantinedError):
        current_guard.assert_epoch(record.recovery_epoch)
    assert store.model_usage(h.workspace, h.first.id, record.id) == recovered


@pytest.mark.postgres
def test_postgres_usage_epoch_survives_restart_and_rejects_direct_sql_rewrite(postgres_url):
    import psycopg

    from simon.adapters.postgres import PostgresStore

    store = PostgresStore(postgres_url, pool_size=2)
    try:
        h = model_records(store)
        record = usage(h, recovery_epoch=str(uuid4()), status="unknown")
        store.insert_model_usage(record)
    finally:
        store.close()
    restarted = PostgresStore(postgres_url, pool_size=2)
    try:
        assert restarted.model_usage(h.workspace, h.first.id, record.id) == record
        for epoch in (str(uuid4()), None):
            with pytest.raises(psycopg.IntegrityError), restarted.transaction():
                restarted.connection.execute(
                    "UPDATE model_usage SET snapshot=jsonb_set(snapshot,'{recovery_epoch}',"
                    "COALESCE(to_jsonb(%s::text),'null'::jsonb)) WHERE id=%s",
                    (epoch, record.id),
                )
        assert restarted.model_usage(h.workspace, h.first.id, record.id) == record
    finally:
        restarted.close()


@pytest.mark.postgres
def test_recovery_migration_preserves_legacy_usage_and_old_reservations(
    empty_native_schema,
    tmp_path,
):
    import psycopg

    from simon.adapters.postgres import PostgresStore
    from simon.migrate import migrate, migration_directory

    predecessor, target = tmp_path / "through_0037", tmp_path / "through_0038"
    predecessor.mkdir()
    target.mkdir()
    migration = "0038_recovery_epochs.sql"
    for path in migration_directory().glob("*.sql"):
        if path.name.endswith(".down.sql") or path.name > migration:
            continue
        (target / path.name).write_bytes(path.read_bytes())
        if path.name < migration:
            (predecessor / path.name).write_bytes(path.read_bytes())
    migrate(empty_native_schema, predecessor)
    store = PostgresStore(empty_native_schema, pool_size=2)
    try:
        h = model_records(store)
        record = expired_usage(h)
        store.insert_model_usage(record)
        with store.transaction():
            store.connection.execute(
                "UPDATE model_usage SET snapshot=snapshot-'recovery_epoch' WHERE id=%s",
                (record.id,),
            )
            before = store.connection.execute(
                "SELECT snapshot FROM model_usage WHERE id=%s",
                (record.id,),
            ).fetchone()["snapshot"]
        assert "recovery_epoch" not in before
        assert migrate(empty_native_schema, target) == [migration]
        with store.transaction():
            after = store.connection.execute(
                "SELECT snapshot FROM model_usage WHERE id=%s",
                (record.id,),
            ).fetchone()["snapshot"]
        assert after == {**before, "recovery_epoch": None}
        assert store.model_usage(h.workspace, h.first.id, record.id).recovery_epoch is None
        guard = active_guard(tmp_path / "new-operator-witness" / "recovery.json")
        service = ModelUsageService(store, NativeProjectService(store), recovery=guard)
        with store.transaction(h.workspace):
            service.recover(h.workspace)
        recovered = store.model_usage(h.workspace, h.first.id, record.id)
        assert recovered.status == "unknown" and recovered.held_microusd == record.held_microusd
        assert store.model_usage_totals(h.workspace, None, utc_now()).active_calls == 1
        with pytest.raises(psycopg.IntegrityError), store.transaction():
            store.connection.execute(
                "UPDATE model_usage SET snapshot=jsonb_set(snapshot,'{recovery_epoch}',"
                "to_jsonb(%s::text)) WHERE id=%s",
                (guard.epoch, record.id),
            )
        assert migrate(empty_native_schema, target) == []
    finally:
        store.close()


@pytest.mark.parametrize("pending", [0, 1, "query_failed"])
def test_recovery_cli_requires_no_pending_usage_and_always_closes_store(monkeypatch, pending):
    import simon.adapters.postgres as postgres_module
    from scripts.recovery import require_reconciled_usage
    from simon.config import Settings

    class FakeStore:
        closed = False

        def recovery_usage_pending(self):
            if pending == "query_failed":
                raise RuntimeError("Injected accounting query failure")
            return pending

        def close(self):
            self.closed = True

    store = FakeStore()
    monkeypatch.setattr(postgres_module, "PostgresStore", lambda _: store)
    settings = Settings(
        storage_backend="postgres", database_url="postgresql://fixture/restore_test"
    )
    if pending == 1:
        with pytest.raises(ValueError, match="Reconcile all restored"):
            require_reconciled_usage(settings)
    elif pending == "query_failed":
        with pytest.raises(RuntimeError, match="accounting query"):
            require_reconciled_usage(settings)
    else:
        require_reconciled_usage(settings)
    assert store.closed
