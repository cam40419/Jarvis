"""Native board migration acceptance using isolated schemas in the disposable test DB."""

from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from simon.adapters.postgres import PostgresStore
from simon.domain.identity import Membership
from simon.domain.models import ActorContext, Channel
from simon.domain.native_projects import CreateNativeProject, CreateNativeTask
from simon.migrate import migrate, migration_directory
from simon.services.identity import ROLE_SCOPES
from simon.services.native_projects import NativeProjectService

pytestmark = pytest.mark.postgres
NATIVE_MIGRATION = "0031_native_agents.sql"


@pytest.fixture
def empty_native_schema(postgres_base_url):
    # postgres_base_url already requires an explicitly configured database ending in _test.
    schema = "simon_test_native_" + uuid4().hex
    url = make_conninfo(postgres_base_url, options=f"-c search_path={schema},public")
    with psycopg.connect(postgres_base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            yield url
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def native_migrations(tmp_path):
    predecessor = tmp_path / "through_0030"
    target = tmp_path / "through_0031"
    predecessor.mkdir()
    target.mkdir()
    for source in migration_directory().glob("*.sql"):
        if source.name.endswith(".down.sql") or source.name > NATIVE_MIGRATION:
            continue
        (target / source.name).write_bytes(source.read_bytes())
        if source.name < NATIVE_MIGRATION:
            (predecessor / source.name).write_bytes(source.read_bytes())
    assert (target / NATIVE_MIGRATION).is_file()
    return predecessor, target


def table_rows(url, names):
    with psycopg.connect(url) as connection:
        return {
            name: connection.execute(
                sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY to_jsonb(t)::text").format(
                    sql.Identifier(name)
                )
            ).fetchall()
            for name in names
        }


def create_workspace(store):
    membership = Membership(actor_id=uuid4(), workspace_id=uuid4(), role="owner")
    store.put_membership(membership)
    return ActorContext(
        actor_id=membership.actor_id,
        workspace_id=membership.workspace_id,
        channel=Channel.API,
        scopes=ROLE_SCOPES["owner"],
    )


def test_fresh_native_migration_and_replay_preserve_created_board(
    empty_native_schema, native_migrations
):
    _, target = native_migrations
    expected = sorted(path.name for path in target.glob("*.sql"))
    assert migrate(empty_native_schema, target) == expected
    with psycopg.connect(empty_native_schema) as connection:
        for name in (
            "native_projects",
            "native_project_members",
            "native_tasks",
            "native_agents",
            "native_team_policies",
            "native_agent_credentials",
        ):
            assert connection.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))
            ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema=current_schema() "
            "AND table_name IN ('project_artifacts','project_file_operations','project_drive')"
        ).fetchone() == (0,)
    store = PostgresStore(empty_native_schema)
    try:
        actor = create_workspace(store)
        service = NativeProjectService(store)
        project = service.create_project(
            actor,
            CreateNativeProject(
                name="Fresh workspace", objective="Coordinate work", idempotency_key="new-project"
            ),
        )
        task = service.create_task(
            actor,
            project.id,
            CreateNativeTask(title="Prepare review", idempotency_key="new-board-task"),
        )
        names = (
            "schema_migrations",
            "native_projects",
            "native_project_members",
            "native_tasks",
            "idempotency_records",
            "audit_events",
            "outbox_events",
        )
        before = table_rows(empty_native_schema, names)
        assert migrate(empty_native_schema, target) == []
        assert table_rows(empty_native_schema, names) == before
        assert service.get_task(actor, project.id, task.id) == task
    finally:
        store.close()


def test_agent_upgrade_keeps_existing_native_human_and_pool_assignments(
    empty_native_schema, native_migrations
):
    predecessor, target = native_migrations
    migrate(empty_native_schema, predecessor)
    workspace, owner, project = uuid4(), uuid4(), uuid4()
    with psycopg.connect(empty_native_schema) as connection:
        connection.execute(
            "INSERT INTO workspaces(id,name) VALUES (%s,'Board workspace')", (workspace,)
        )
        connection.execute("INSERT INTO users(id,display_name) VALUES (%s,'Board owner')", (owner,))
        connection.execute(
            "INSERT INTO memberships(workspace_id,user_id,role) VALUES (%s,%s,'owner')",
            (workspace, owner),
        )
        connection.execute(
            "INSERT INTO native_projects(id,workspace_id,name,objective,status,board_authority,"
            "created_by,version,created_at,updated_at) "
            "VALUES (%s,%s,'Existing board','Keep native work','active','native',%s,1,now(),now())",
            (project, workspace, owner),
        )
        connection.execute(
            "INSERT INTO native_project_members(workspace_id,project_id,actor_id,role,created_at) "
            "VALUES (%s,%s,%s,'owner',now())",
            (workspace, project, owner),
        )
        for kind, assignee in (("human", owner), ("pool", None)):
            connection.execute(
                "INSERT INTO native_tasks(id,workspace_id,project_id,title,description,status,"
                "assignment_kind,assignee_actor_id,created_by,version,created_at,updated_at) "
                "VALUES (%s,%s,%s,'Existing task','','todo',%s,%s,%s,1,now(),now())",
                (uuid4(), workspace, project, kind, assignee, owner),
            )
    before = table_rows(
        empty_native_schema, ("native_projects", "native_project_members", "native_tasks")
    )
    assert migrate(empty_native_schema, target) == [NATIVE_MIGRATION]
    after = table_rows(empty_native_schema, tuple(before))
    assert after["native_projects"] == before["native_projects"]
    assert after["native_project_members"] == before["native_project_members"]
    updated_tasks = []
    for (row,) in after["native_tasks"]:
        assert row.pop("assignee_agent_id") is None
        assert row.pop("created_by_agent_id") is None
        updated_tasks.append(row)
    assert sorted(updated_tasks, key=lambda row: row["id"]) == sorted(
        [row for (row,) in before["native_tasks"]], key=lambda row: row["id"]
    )


def test_failed_native_migration_rolls_back_its_tables_and_can_retry(
    empty_native_schema, native_migrations
):
    predecessor, target = native_migrations
    migrate(empty_native_schema, predecessor)
    before = table_rows(empty_native_schema, ("schema_migrations",))
    # A collision in the final CREATE TABLE proves earlier DDL rolls back with its ledger entry.
    with psycopg.connect(empty_native_schema) as connection:
        connection.execute("CREATE TABLE native_agent_credentials (sentinel text PRIMARY KEY)")
        connection.execute("INSERT INTO native_agent_credentials VALUES ('existing object')")
    with pytest.raises(psycopg.errors.DuplicateTable):
        migrate(empty_native_schema, target)
    assert table_rows(empty_native_schema, ("schema_migrations",)) == before
    with psycopg.connect(empty_native_schema) as connection:
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema=current_schema() "
            "AND table_name IN ('native_agents','native_team_policies')"
        ).fetchone() == (0,)
        assert connection.execute("SELECT sentinel FROM native_agent_credentials").fetchone() == (
            "existing object",
        )
        connection.execute("DROP TABLE native_agent_credentials")
    assert migrate(empty_native_schema, target) == [NATIVE_MIGRATION]
    assert migrate(empty_native_schema, target) == []
