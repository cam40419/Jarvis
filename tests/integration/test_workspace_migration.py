from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from simon.migrate import migrate, migration_directory

pytestmark = pytest.mark.postgres


def test_historical_workspace_rename_preserves_sql_rows_and_ledger(postgres_base_url, tmp_path):
    schema = "simon_test_" + uuid4().hex
    workspace, actor, thread = uuid4(), uuid4(), uuid4()
    legacy_dir = tmp_path / "migrations"
    legacy_dir.mkdir()
    for path in migration_directory().glob("*.sql"):
        if path.name < "0028" and not path.name.endswith(".down.sql"):
            (legacy_dir / path.name).write_bytes(path.read_bytes())
    url = make_conninfo(postgres_base_url, options=f"-c search_path={schema},public")
    with psycopg.connect(postgres_base_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            migrate(url, legacy_dir)
            snapshot = {
                "id": "clickup-test",
                "provider": "clickup",
                "name": "Saved connection",
                "household_id": str(workspace),
                "actor_id": str(actor),
                "encrypted_secret": "unchanged-ciphertext",
                "settings": {},
            }
            with psycopg.connect(url) as connection:
                connection.execute(
                    "INSERT INTO households(id,name) VALUES (%s,'Saved workspace')", (workspace,)
                )
                connection.execute(
                    "INSERT INTO users(id,display_name) VALUES (%s,'Saved user')", (actor,)
                )
                connection.execute(
                    "INSERT INTO memberships(household_id,user_id,role) VALUES (%s,%s,'owner')",
                    (workspace, actor),
                )
                connection.execute(
                    "INSERT INTO threads(id,household_id,created_by,title) "
                    "VALUES (%s,%s,%s,'Saved thread')",
                    (thread, workspace, actor),
                )
                connection.execute(
                    "INSERT INTO auth_sessions(token_hash,actor_id,household_id,method,"
                    "created_at,expires_at) VALUES "
                    "('saved-session',%s,%s,'password',now(),now()+interval '1 day')",
                    (actor, workspace),
                )
                connection.execute(
                    "INSERT INTO integration_connections(household_id,actor_id,id,snapshot) "
                    "VALUES (%s,%s,'clickup-test',%s)",
                    (workspace, actor, Jsonb(snapshot)),
                )
                connection.execute(
                    "INSERT INTO audit_events(household_id,sequence,event_type,actor_id,"
                    "correlation_id,resource_type,resource_id,payload,previous_hash,event_hash) "
                    "VALUES (%s,1,'test.saved',%s,%s,'user','saved',%s,%s,%s)",
                    (
                        workspace,
                        actor,
                        uuid4(),
                        Jsonb({"household_id": str(workspace)}),
                        "0" * 64,
                        "a" * 64,
                    ),
                )
            assert migrate(url) == [
                "0028_workspace_identity.sql",
                "0029_email_password_recovery.sql",
                "0030_native_projects.sql",
                "0031_native_agents.sql",
            ]
            assert migrate(url) == []
            with psycopg.connect(url) as connection:
                # Historical migrations remain an immutable SQL ledger. This does not promise
                # that current application models decode the retired snapshot format.
                assert (
                    connection.execute(
                        "SELECT workspace_id FROM memberships WHERE user_id=%s", (actor,)
                    ).fetchone()[0]
                    == workspace
                )
                assert (
                    connection.execute(
                        "SELECT workspace_id FROM auth_sessions WHERE token_hash='saved-session'"
                    ).fetchone()[0]
                    == workspace
                )
                assert (
                    connection.execute(
                        "SELECT visibility FROM threads WHERE id=%s", (thread,)
                    ).fetchone()[0]
                    == "workspace"
                )
                assert connection.execute(
                    "SELECT event_hash,payload FROM audit_events WHERE workspace_id=%s",
                    (workspace,),
                ).fetchone() == ("a" * 64, {"household_id": str(workspace)})
                assert (
                    connection.execute("SELECT snapshot FROM integration_connections").fetchone()[0]
                    == snapshot
                )
                assert (
                    connection.execute("SELECT name FROM workspaces").fetchone()[0]
                    == "Saved workspace"
                )
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
