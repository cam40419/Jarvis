"""Resource authority replaces intake-only settings without losing historical liabilities."""

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from simon.adapters.postgres import PostgresStore
from simon.domain.native_intake import IntakeRun, NativeIntake
from simon.domain.native_models import ModelUsage
from simon.migrate import migrate, migration_directory
from tests.contract.test_native_project_store import seed_native_projects
from tests.integration.test_native_project_migration import (
    empty_native_schema as empty_native_schema,
)
from tests.integration.test_native_project_migration import table_rows

pytestmark = pytest.mark.postgres
MODEL_MIGRATION = "0033_project_models.sql"


@pytest.fixture
def model_migrations(tmp_path):
    predecessor, target = tmp_path / "through_0032", tmp_path / "through_0033"
    predecessor.mkdir()
    target.mkdir()
    for source in migration_directory().glob("*.sql"):
        if source.name.endswith(".down.sql") or source.name > MODEL_MIGRATION:
            continue
        (target / source.name).write_bytes(source.read_bytes())
        if source.name < MODEL_MIGRATION:
            (predecessor / source.name).write_bytes(source.read_bytes())
    return predecessor, target


def test_model_migration_preserves_intake_evidence_and_imports_liabilities_once(
    empty_native_schema, model_migrations
):
    predecessor, target = model_migrations
    migrate(empty_native_schema, predecessor)
    store = PostgresStore(empty_native_schema, pool_size=2)
    try:
        h = seed_native_projects(store)
        intake = NativeIntake(
            workspace_id=h.workspace,
            project_id=h.first.id,
            version=1,
            outcomes="Preserve outcomes",
            answers={"audience": "Artists"},
        )
        store.save_native_intake(intake, 0)
        with store.transaction():
            store.connection.execute(
                "UPDATE native_intakes SET snapshot=snapshot || %s WHERE project_id=%s",
                (
                    Jsonb(
                        {
                            "endpoint_id": "retired-catalog",
                            "allow_cloud": True,
                            "budget_microusd": 2_000_000,
                        }
                    ),
                    h.first.id,
                ),
            )
        now = datetime(2026, 10, 8, 12, tzinfo=UTC)
        runs = []
        for index, (charged, held) in enumerate(((120, 0), (0, 300), (25, 200), (0, 0))):
            value = IntakeRun(
                workspace_id=h.workspace,
                project_id=h.first.id,
                requested_by=h.owner,
                idempotency_key=f"historical-plan-{index}",
                request_digest="a" * 64,
                snapshot_digest="b" * 64,
                intake_version=1,
                endpoint_id="retired-catalog",
                model="historical-model",
                charged_microusd=charged,
                reserved_microusd=held,
                status="unknown" if held else "ready",
                started_at=now,
                deadline_at=now + timedelta(minutes=3),
                finished_at=now + timedelta(seconds=1),
            )
            store.insert_native_intake_run(value)
            with store.transaction():
                store.connection.execute(
                    "UPDATE native_intake_runs SET snapshot=snapshot "
                    "-'usage_ids'-'review_endpoint_id'-'review_model' WHERE id=%s",
                    (value.id,),
                )
            runs.append(value)
        boards = table_rows(empty_native_schema, ("native_projects", "native_project_members"))
        assert migrate(empty_native_schema, target) == [MODEL_MIGRATION]
        assert (
            table_rows(empty_native_schema, ("native_projects", "native_project_members")) == boards
        )
        assert store.native_intake(h.workspace, h.first.id) == intake
        assert store.project_models(h.workspace, h.first.id) == ()
        assert store.model_resource_policy(h.workspace, h.first.id) is None
        usages = store.model_usage_entries(h.workspace)
        assert len(usages) == len(runs)
        assert store.model_usage_totals(h.workspace, None, now).charged_lifetime == 145
        assert store.model_usage_totals(h.workspace, None, now).held_microusd == 500
        assert store.model_usage_totals(h.workspace, None, now).active_calls == 2
        for previous in runs:
            imported = store.model_usage(h.workspace, h.first.id, previous.id)
            assert isinstance(imported, ModelUsage)
            assert imported.operation_id == previous.id
            assert imported.phase == "intake_import"
            assert imported.model_id is None and imported.model_version == 0
            assert imported.endpoint_snapshot == {}
            assert (
                imported.reserved_microusd == previous.reserved_microusd + previous.charged_microusd
            )
            assert imported.held_microusd == previous.reserved_microusd
            assert imported.charged_microusd == previous.charged_microusd
            assert imported.status == ("unknown" if previous.reserved_microusd else "settled")
            restored = store.native_intake_run(h.workspace, h.first.id, previous.id)
            assert restored.usage_ids == (imported.id,)
            assert restored.review_model is None and restored.review_endpoint_id is None
            # New defaults must be included in old snapshots for current CAS to work.
            updated = restored.model_copy(update={"version": 2, "status": "stale"})
            store.update_native_intake_run(updated, 1)
        assert migrate(empty_native_schema, target) == []
        assert store.model_usage_entries(h.workspace) == usages
        store.close()
        assert store.model_usage_totals(h.workspace, None, now).held_microusd == 500
    finally:
        store.close()


def test_failed_model_migration_rolls_back_and_retries(empty_native_schema, model_migrations):
    predecessor, target = model_migrations
    migrate(empty_native_schema, predecessor)
    before = table_rows(empty_native_schema, ("schema_migrations",))
    with psycopg.connect(empty_native_schema) as connection:
        connection.execute("CREATE TABLE model_usage (sentinel text PRIMARY KEY)")
        connection.execute("INSERT INTO model_usage VALUES ('preserve preexisting object')")
    with pytest.raises(psycopg.errors.DuplicateTable):
        migrate(empty_native_schema, target)
    assert table_rows(empty_native_schema, ("schema_migrations",)) == before
    with psycopg.connect(empty_native_schema) as connection:
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_name IN ('project_models',"
            "'project_model_credentials','workspace_model_policies','project_model_policies')"
        ).fetchone() == (0,)
        assert connection.execute("SELECT sentinel FROM model_usage").fetchone() == (
            "preserve preexisting object",
        )
        connection.execute("DROP TABLE model_usage")
    assert migrate(empty_native_schema, target) == [MODEL_MIGRATION]
    assert migrate(empty_native_schema, target) == []
