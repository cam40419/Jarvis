"""The intake migration is additive, repeatable, and rolls back incomplete DDL."""

import psycopg
import pytest

from simon.adapters.postgres import PostgresStore
from simon.domain.native_agents import NativeAgent
from simon.domain.native_projects import NativeTask
from simon.migrate import migrate, migration_directory
from tests.contract.test_native_project_store import seed_native_projects
from tests.integration.test_native_project_migration import (
    empty_native_schema as empty_native_schema,
)
from tests.integration.test_native_project_migration import table_rows

pytestmark = pytest.mark.postgres
INTAKE_MIGRATION = "0032_native_intake.sql"


@pytest.fixture
def intake_migrations(tmp_path):
    predecessor = tmp_path / "through_0031"
    target = tmp_path / "through_0032"
    predecessor.mkdir()
    target.mkdir()
    for source in migration_directory().glob("*.sql"):
        if source.name.endswith(".down.sql") or source.name > INTAKE_MIGRATION:
            continue
        (target / source.name).write_bytes(source.read_bytes())
        if source.name < INTAKE_MIGRATION:
            (predecessor / source.name).write_bytes(source.read_bytes())
    return predecessor, target


def test_intake_upgrade_and_replay_preserve_boards_and_staff(
    empty_native_schema, intake_migrations
):
    predecessor, target = intake_migrations
    migrate(empty_native_schema, predecessor)
    store = PostgresStore(empty_native_schema, pool_size=2)
    try:
        h = seed_native_projects(store)
        store.insert_native_agent(
            NativeAgent(
                workspace_id=h.workspace,
                project_id=h.first.id,
                role_key="researcher",
                name="Researcher",
                instructions="Cite evidence",
                success_criteria="Grounded claims",
                rationale="Review project evidence",
                created_by=h.owner,
            )
        )
        store.insert_native_task(
            NativeTask(
                workspace_id=h.workspace,
                project_id=h.first.id,
                title="Preserve existing work",
                created_by=h.owner,
            )
        )
        names = ("native_projects", "native_project_members", "native_agents", "native_tasks")
        before = table_rows(empty_native_schema, names)
        assert migrate(empty_native_schema, target) == [INTAKE_MIGRATION]
        assert table_rows(empty_native_schema, names) == before
        assert table_rows(
            empty_native_schema, ("native_intakes", "native_intake_sources", "native_intake_runs")
        ) == {"native_intakes": [], "native_intake_sources": [], "native_intake_runs": []}
        ledger = table_rows(empty_native_schema, ("schema_migrations",))
        assert migrate(empty_native_schema, target) == []
        assert table_rows(empty_native_schema, ("schema_migrations",)) == ledger
    finally:
        store.close()


def test_failed_intake_migration_rolls_back_and_retries(empty_native_schema, intake_migrations):
    predecessor, target = intake_migrations
    migrate(empty_native_schema, predecessor)
    before = table_rows(empty_native_schema, ("schema_migrations",))
    with psycopg.connect(empty_native_schema) as connection:
        connection.execute("CREATE TABLE native_intake_runs (sentinel text PRIMARY KEY)")
        connection.execute("INSERT INTO native_intake_runs VALUES ('preserve existing object')")
    with pytest.raises(psycopg.errors.DuplicateTable):
        migrate(empty_native_schema, target)
    assert table_rows(empty_native_schema, ("schema_migrations",)) == before
    with psycopg.connect(empty_native_schema) as connection:
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema=current_schema() "
            "AND table_name IN ('native_intakes','native_intake_sources')"
        ).fetchone() == (0,)
        assert connection.execute("SELECT sentinel FROM native_intake_runs").fetchone() == (
            "preserve existing object",
        )
        connection.execute("DROP TABLE native_intake_runs")
    assert migrate(empty_native_schema, target) == [INTAKE_MIGRATION]
    assert migrate(empty_native_schema, target) == []
