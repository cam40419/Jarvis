"""Match the backup database exactly, then defer complete backups while compute is active."""

import subprocess
from pathlib import Path

from simon.api.app import AppContainer
from simon.config import Settings

IDENTITY_QUERY = "SELECT current_database(), system_identifier::text FROM pg_control_system()"


def configured_identity(settings: Settings) -> tuple[str, str]:
    import psycopg

    with psycopg.connect(settings.database_url.get_secret_value(), connect_timeout=5) as connection:
        row = connection.execute(IDENTITY_QUERY).fetchone()
    if row is None or len(row) != 2:
        raise ValueError("Database identity was not returned")
    return str(row[0]), str(row[1])


def compose_identity() -> tuple[str, str]:
    compose = Path(__file__).resolve().parents[1] / "deploy/compose/compose.yaml"
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-U",
            "jarvis",
            "-d",
            "jarvis",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-F",
            "|",
            "-c",
            IDENTITY_QUERY,
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    parts = result.stdout.decode().strip().split("|")
    if len(parts) != 2:
        raise ValueError("Compose database identity was not returned")
    return parts[0], parts[1]


def validate_target(settings: Settings) -> None:
    if settings.storage_backend != "postgres":
        raise ValueError("Full backups require the PostgreSQL storage backend")
    if configured_identity(settings) != compose_identity():
        raise ValueError("Configured and Compose database identities differ")


def main() -> int:
    try:
        validate_target(Settings())
    except Exception:
        print("Full backup refused: could not match the configured database to the Compose target.")
        return 2
    container = AppContainer()
    try:
        busy = any(
            container.store.jobs_all(kind, 1, status)
            for kind in ("assistant.session",)
            for status in ("queued", "running")
        )
        print("Full backup deferred while work is active." if busy else "Backup window is idle.")
        return 10 if busy else 0
    finally:
        container.store.close()


if __name__ == "__main__":
    raise SystemExit(main())
