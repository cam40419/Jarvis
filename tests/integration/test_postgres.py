from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from jarvis.api.app import AppContainer, create_app
from jarvis.config import Settings

pytestmark = pytest.mark.postgres


@pytest.fixture
def seeded_url(postgres_url: str) -> str:
    from jarvis.seed import seed_development_identity

    seed_development_identity(postgres_url)
    return postgres_url


def test_api_state_survives_new_container(seeded_url: str) -> None:
    from pydantic import SecretStr

    settings = Settings(
        storage_backend="postgres",
        database_url=SecretStr(seeded_url),
        dev_login_enabled=True,
        dev_login_token=SecretStr("postgres-development-secret-32-characters"),
    )
    origin = "http://localhost:8000"
    invocation = {
        "capability": "system.echo",
        "arguments": {"message": "durable"},
        "idempotency_key": "restart-echo-001",
    }
    body = {"kind": "test.job", "input": {"value": 1}, "idempotency_key": "restart-job-001"}
    with TestClient(create_app(AppContainer(settings=settings)), base_url=origin) as client:
        login = client.post(
            "/auth/dev-login",
            headers={"Origin": origin},
            json={"token": "postgres-development-secret-32-characters"},
        )
        assert login.status_code == 200
        headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        cookies = dict(client.cookies)
        first = client.post("/v1/capabilities/invoke", headers=headers, json=invocation)
        assert first.status_code == 200
        submitted = client.post("/v1/jobs", headers=headers, json=body)
        assert submitted.status_code == 202
        job = submitted.json()
    restarted = AppContainer(settings=settings)
    with TestClient(create_app(restarted), base_url=origin, cookies=cookies) as client:
        replay = client.post("/v1/capabilities/invoke", headers=headers, json=invocation).json()
        assert replay["replayed"] is True
        assert replay["invocation_id"] == first.json()["invocation_id"]
        assert client.get(f"/v1/jobs/{job['id']}", headers=headers).json() == job
        assert client.post("/v1/jobs", headers=headers, json=body).json() == job
        assert (
            client.get(
                "/auth/session",
                headers={
                    "X-Actor-Id": str(uuid4()),
                    "X-Household-Id": str(uuid4()),
                    "X-Scopes": "dangerous:all",
                },
            ).json()["actor_id"]
            == login.json()["actor_id"]
        )
    assert len(restarted.store.audit_events()) == len(restarted.store.outbox_events()) == 3


def test_migration_replay_and_changed_checksum(postgres_url: str, tmp_path: Path) -> None:
    from jarvis.migrate import migrate, migration_directory

    assert migrate(postgres_url) == []
    for source in migration_directory().glob("*.sql"):
        (tmp_path / source.name).write_text(source.read_text())
    foundation = tmp_path / "0001_foundation.sql"
    foundation.write_text(foundation.read_text() + "\n-- changed\n")
    with pytest.raises(ValueError, match="checksum"):
        migrate(postgres_url, tmp_path)


def test_failed_migration_rolls_back_ddl_and_version(postgres_url: str, tmp_path: Path) -> None:
    import psycopg

    from jarvis.migrate import migrate, migration_directory

    for source in migration_directory().glob("*.sql"):
        (tmp_path / source.name).write_text(source.read_text())
    failing = tmp_path / "9999_failure.sql"
    failing.write_text("CREATE TABLE rollback_probe (id integer); SELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(postgres_url, tmp_path)
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT to_regclass('rollback_probe')").fetchone() == (None,)
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE name = '9999_failure.sql'"
        ).fetchone() == (0,)
    failing.write_text("CREATE TABLE rollback_probe (id integer);")
    assert migrate(postgres_url, tmp_path) == ["9999_failure.sql"]
    with pytest.raises(ValueError, match="unknown"):
        migrate(postgres_url)


def test_no_migration_files_is_an_error(tmp_path: Path) -> None:
    from jarvis.migrate import migrate

    with pytest.raises(ValueError, match="no migration"):
        migrate("unused", tmp_path)


def test_seed_is_repeatable(seeded_url: str) -> None:
    import psycopg

    from jarvis.seed import seed_development_identity

    seed_development_identity(seeded_url)
    with psycopg.connect(seeded_url) as connection:
        assert connection.execute("SELECT count(*) FROM memberships").fetchone() == (1,)
