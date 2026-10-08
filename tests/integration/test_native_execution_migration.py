"""Execution migration rollback, relational boundaries and process-restart durability."""

from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from simon.adapters.postgres import PostgresStore
from simon.domain.models import utc_now
from simon.domain.native_execution import (
    NativeExecutionEvent,
    NativeExecutionRunner,
    NativeExecutionSchedule,
    NativeExecutionSignal,
    NativeExecutionWait,
    NativeTaskWorkflow,
)
from simon.migrate import migrate, migration_directory
from tests.contract.test_native_execution_store import run_record, seed_execution, step_record
from tests.contract.test_native_project_store import seed_native_projects
from tests.integration.test_native_project_migration import (
    empty_native_schema as empty_native_schema,
)
from tests.integration.test_native_project_migration import table_rows

pytestmark = pytest.mark.postgres
EXECUTION_MIGRATION = "0034_native_execution.sql"


@pytest.fixture
def execution_migrations(tmp_path):
    predecessor, target = tmp_path / "through_0033", tmp_path / "through_0034"
    predecessor.mkdir()
    target.mkdir()
    for source in migration_directory().glob("*.sql"):
        if source.name.endswith(".down.sql") or source.name > EXECUTION_MIGRATION:
            continue
        (target / source.name).write_bytes(source.read_bytes())
        if source.name < EXECUTION_MIGRATION:
            (predecessor / source.name).write_bytes(source.read_bytes())
    return predecessor, target


def test_execution_upgrade_preserves_board_and_replay(empty_native_schema, execution_migrations):
    predecessor, target = execution_migrations
    migrate(empty_native_schema, predecessor)
    store = PostgresStore(empty_native_schema, pool_size=2)
    try:
        seed_native_projects(store)
        tables = ("native_projects", "native_project_members", "native_tasks", "project_models")
        before = table_rows(empty_native_schema, tables)
        assert migrate(empty_native_schema, target) == [EXECUTION_MIGRATION]
        assert table_rows(empty_native_schema, tables) == before
        assert store.execution_project_scopes() == ()
        assert migrate(empty_native_schema, target) == []
    finally:
        store.close()


def test_execution_migration_rolls_back_all_objects_then_retries(
    empty_native_schema, execution_migrations
):
    predecessor, target = execution_migrations
    migrate(empty_native_schema, predecessor)
    before = table_rows(empty_native_schema, ("schema_migrations",))
    with psycopg.connect(empty_native_schema) as connection:
        connection.execute("CREATE TABLE native_execution_steps (sentinel text PRIMARY KEY)")
        connection.execute("INSERT INTO native_execution_steps VALUES ('preserve')")
    with pytest.raises(psycopg.errors.DuplicateTable):
        migrate(empty_native_schema, target)
    assert table_rows(empty_native_schema, ("schema_migrations",)) == before
    with psycopg.connect(empty_native_schema) as connection:
        assert connection.execute("SELECT sentinel FROM native_execution_steps").fetchall() == [
            ("preserve",)
        ]
        assert (
            connection.execute("SELECT to_regclass('native_execution_runs')").fetchone()[0] is None
        )
        connection.execute("DROP TABLE native_execution_steps")
    assert migrate(empty_native_schema, target) == [EXECUTION_MIGRATION]


def test_every_execution_record_survives_store_restart(postgres_url):
    store = PostgresStore(postgres_url, pool_size=2)
    try:
        h = seed_execution(store)
        now = utc_now()
        workflow = NativeTaskWorkflow(
            workspace_id=h.workspace,
            project_id=h.first.id,
            task_id=h.task.id,
            version=1,
            dependency_ids=(h.other.id,),
        )
        store.save_task_workflow(workflow, 0)
        schedule = NativeExecutionSchedule(
            workspace_id=h.workspace,
            project_id=h.first.id,
            task_id=h.task.id,
            issued_by=h.owner,
            next_run_at=now,
        )
        store.insert_execution_schedule(schedule)
        runner = NativeExecutionRunner(
            workspace_id=h.workspace,
            project_id=h.first.id,
            issued_by=h.owner,
            name="Outbound",
            token_hash="b" * 64,
            expires_at=now + timedelta(days=1),
        )
        store.insert_execution_runner(runner)
        run = run_record(
            h,
            runner_id=runner.id,
            status="unknown",
            fence=1,
            attempt=1,
            lease_token_hash="c" * 64,
            lease_until=now + timedelta(minutes=5),
            schedule_id=schedule.id,
            schedule_definition_version=1,
        )
        store.insert_execution_run(run)
        step = step_record(h, run, status="unknown")
        store.insert_execution_step(step)
        event = NativeExecutionEvent(
            workspace_id=h.workspace,
            project_id=h.first.id,
            run_id=run.id,
            sequence=1,
            kind="dispatch_unknown",
        )
        store.append_execution_event(event)
        wait = NativeExecutionWait(
            workspace_id=h.workspace,
            project_id=h.first.id,
            run_id=run.id,
            kind="human",
            correlation_id=uuid4(),
            deadline_at=now + timedelta(hours=1),
        )
        store.insert_execution_wait(wait)
        signal = NativeExecutionSignal(
            workspace_id=h.workspace,
            project_id=h.first.id,
            run_id=run.id,
            correlation_id=wait.correlation_id,
            received_by=h.owner,
            text="Recorded once",
        )
        store.insert_execution_signal(signal)
    finally:
        store.close()
    restored = PostgresStore(postgres_url, pool_size=2)
    try:
        assert restored.execution_policy(h.workspace, h.first.id) == h.policy
        assert restored.task_workflow(h.workspace, h.first.id, h.task.id) == workflow
        assert restored.execution_schedule(h.workspace, h.first.id, schedule.id) == schedule
        assert restored.execution_runner_by_hash(runner.token_hash) == runner
        assert restored.execution_run(h.workspace, h.first.id, run.id) == run
        assert restored.execution_step(h.workspace, h.first.id, step.id) == step
        assert restored.execution_events(h.workspace, h.first.id, run.id) == (event,)
        assert restored.execution_waits(h.workspace, h.first.id, run.id) == (wait,)
        assert (
            restored.execution_signal(h.workspace, h.first.id, run.id, wait.correlation_id)
            == signal
        )
        # Restarting a store does not claim, renew, finish or resubmit an unknown call.
        assert restored.execution_active_runs(h.workspace, h.first.id) == (run,)
        assert restored.execution_run(h.workspace, h.first.id, run.id).fence == 1
    finally:
        restored.close()


def test_execution_sql_rejects_cross_project_edges_even_without_adapter_validation(postgres_url):
    store = PostgresStore(postgres_url, pool_size=2)
    try:
        h = seed_execution(store)
        run = run_record(h)
        store.insert_execution_run(run)
        # Keep the JSON snapshot consistent with the changed SQL columns to exercise the FK.
        with pytest.raises(psycopg.errors.ForeignKeyViolation), store.transaction(h.workspace):
            store.connection.execute(
                "UPDATE native_execution_runs SET project_id=%s,snapshot=snapshot || %s "
                "WHERE id=%s",
                (h.second.id, Jsonb({"project_id": str(h.second.id)}), run.id),
            )
        config = NativeTaskWorkflow(
            workspace_id=h.workspace, project_id=h.first.id, task_id=h.task.id, version=1
        )
        store.save_task_workflow(config, 0)
        with pytest.raises(psycopg.errors.ForeignKeyViolation), store.transaction(h.workspace):
            store.connection.execute(
                "INSERT INTO native_task_dependencies "
                "(workspace_id,project_id,task_id,dependency_id) "
                "VALUES (%s,%s,%s,%s)",
                (h.workspace, h.first.id, h.task.id, uuid4()),
            )
    finally:
        store.close()
