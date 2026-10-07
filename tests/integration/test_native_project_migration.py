"""Native board migration acceptance using isolated schemas in the disposable test DB."""

import hashlib
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from simon.adapters.postgres import PostgresStore
from simon.domain.context import ExplicitMemory
from simon.domain.identity import Membership, Session
from simon.domain.models import ActorContext, Channel, Job, JobStatus, utc_now
from simon.domain.native_projects import CreateNativeProject, CreateNativeTask
from simon.domain.project_files import ProjectDrive
from simon.domain.tasks import ProjectArtifact
from simon.migrate import migrate, migration_directory
from simon.services.audit import AuditService
from simon.services.canonical import digest
from simon.services.identity import ROLE_SCOPES
from simon.services.native_projects import NativeProjectService

pytestmark = pytest.mark.postgres
NATIVE_MIGRATION = "0030_native_projects.sql"


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
    predecessor = tmp_path / "through_0029"
    target = tmp_path / "through_0030"
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
        for name in ("native_projects", "native_project_members", "native_tasks"):
            assert connection.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))
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


def test_upgrade_to_native_board_preserves_legacy_projects_and_records(
    empty_native_schema, native_migrations
):
    predecessor, target = native_migrations
    migrate(empty_native_schema, predecessor)
    store = PostgresStore(empty_native_schema)
    try:
        actor = create_workspace(store)
        legacy_project = ExplicitMemory(
            workspace_id=actor.workspace_id,
            created_by=actor.actor_id,
            subject="Existing private project",
            content="Keep the original project identity and history.",
            scope="personal",
            category="project",
        )
        store.insert_memory(legacy_project)
        legacy_state = {"project_id": str(legacy_project.id), "todos": [{"title": "Saved task"}]}
        job, _ = store.create_job(
            Job(
                workspace_id=actor.workspace_id,
                created_by=actor.actor_id,
                kind="platform.project_work",
                idempotency_key="existing-project-work",
                input={"project_id": str(legacy_project.id), "initial_state": legacy_state},
                input_digest=digest(legacy_state),
                result=legacy_state,
                status=JobStatus.WAITING,
            )
        )
        binding = ProjectDrive(
            workspace_id=actor.workspace_id,
            actor_id=actor.actor_id,
            project_id=legacy_project.id,
            folder_id="saved-folder-id",
            status="ready",
        )
        store.save_project_drive(binding)
        content = b"Original immutable project output."
        artifact = ProjectArtifact(
            id=uuid4(),
            workspace_id=actor.workspace_id,
            actor_id=actor.actor_id,
            project_id=legacy_project.id,
            task_id=job.id,
            name="existing-output.txt",
            media_type="text/plain",
            byte_count=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=utc_now(),
        )
        store.save_project_artifact(artifact, content)
        session = Session(
            token_hash="a" * 64,
            actor_id=actor.actor_id,
            workspace_id=actor.workspace_id,
            method="password",
            expires_at=utc_now() + timedelta(hours=1),
        )
        store.save_session(session)
        store.execute_once("legacy-project", "existing-request", digest({}), lambda: legacy_state)
        AuditService(store).record(
            actor=actor,
            event_type="legacy.saved",
            resource_type="project",
            resource_id=str(legacy_project.id),
            payload=legacy_state,
        )
        preserved = (
            "workspaces",
            "users",
            "memberships",
            "memories",
            "jobs",
            "project_drive",
            "project_artifacts",
            "auth_sessions",
            "audit_events",
            "outbox_events",
            "idempotency_records",
        )
        before = table_rows(empty_native_schema, preserved)
        old_versions = table_rows(empty_native_schema, ("schema_migrations",))["schema_migrations"]
        assert migrate(empty_native_schema, target) == [NATIVE_MIGRATION]
        assert table_rows(empty_native_schema, preserved) == before
        new_versions = table_rows(empty_native_schema, ("schema_migrations",))["schema_migrations"]
        assert [row for row in new_versions if row[0]["name"] != NATIVE_MIGRATION] == old_versions
        assert store.explicit_memory(actor.workspace_id, legacy_project.id) == legacy_project
        assert store.get_job(job.id) == job
        assert store.project_drive(actor.workspace_id, actor.actor_id, legacy_project.id) == binding
        assert store.project_artifact(artifact.id) == (artifact, content)
        assert store.get_session(session.token_hash) == session
        assert store.native_projects(actor.workspace_id, actor.actor_id, 0, 100) == ()
        assert store.native_project(actor.workspace_id, legacy_project.id) is None
        service = NativeProjectService(store)
        native_project = service.create_project(
            actor,
            CreateNativeProject(
                name="New shared project",
                objective="Use native membership and tasks",
                idempotency_key="native-after-upgrade",
            ),
        )
        assert native_project.id != legacy_project.id
        assert store.explicit_memory(actor.workspace_id, native_project.id) is None
        assert store.explicit_memory(actor.workspace_id, legacy_project.id) == legacy_project
        assert migrate(empty_native_schema, target) == []
    finally:
        store.close()


def test_failed_native_migration_rolls_back_its_tables_and_can_retry(
    empty_native_schema, native_migrations
):
    predecessor, target = native_migrations
    migrate(empty_native_schema, predecessor)
    before = table_rows(empty_native_schema, ("schema_migrations",))
    # A collision in the final CREATE TABLE proves earlier DDL rolls back with its ledger entry.
    with psycopg.connect(empty_native_schema) as connection:
        connection.execute("CREATE TABLE native_tasks (sentinel text PRIMARY KEY)")
        connection.execute("INSERT INTO native_tasks VALUES ('existing object')")
    with pytest.raises(psycopg.errors.DuplicateTable):
        migrate(empty_native_schema, target)
    assert table_rows(empty_native_schema, ("schema_migrations",)) == before
    with psycopg.connect(empty_native_schema) as connection:
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema=current_schema() "
            "AND table_name IN ('native_projects','native_project_members')"
        ).fetchone() == (0,)
        assert connection.execute("SELECT sentinel FROM native_tasks").fetchone() == (
            "existing object",
        )
        connection.execute("DROP TABLE native_tasks")
    assert migrate(empty_native_schema, target) == [NATIVE_MIGRATION]
    assert migrate(empty_native_schema, target) == []
