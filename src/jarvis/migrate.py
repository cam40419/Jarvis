"""Apply versioned SQL migrations: python -m jarvis.migrate."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import psycopg

from jarvis.config import get_settings


def migration_directory() -> Path:
    packaged = Path(__file__).parent / "migrations"
    return packaged if packaged.is_dir() else Path(__file__).parents[2] / "db" / "migrations"


def migrate(database_url: str, directory: Path | None = None) -> list[str]:
    directory = directory or migration_directory()
    files = sorted(p for p in directory.glob("*.sql") if not p.name.endswith(".down.sql"))
    if not files:
        raise ValueError("no migration files found")
    applied = []
    with (
        psycopg.connect(database_url, autocommit=True, connect_timeout=5) as connection,
        connection.transaction(),
    ):
        connection.execute("SELECT pg_advisory_xact_lock(741982001)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name text PRIMARY KEY, checksum char(64) NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        )
        rows: dict[str, str] = dict(
            connection.execute("SELECT name, checksum FROM schema_migrations")
        )
        if set(rows) - {path.name for path in files}:
            raise ValueError("database contains unknown migrations")
        for path in files:
            source = path.read_text(encoding="utf-8").replace("\r\n", "\n")
            checksum = hashlib.sha256(source.encode()).hexdigest()
            if path.name in rows:
                if rows[path.name] != checksum:
                    raise ValueError(f"migration checksum changed: {path.name}")
                continue
            # The original foundation file is also executable directly in psql.
            # The runner owns the transaction, including its version record.
            sql = source.strip()
            if sql.startswith("BEGIN;") and sql.endswith("COMMIT;"):
                sql = sql[len("BEGIN;") : -len("COMMIT;")]
            connection.execute(sql)
            connection.execute(
                "INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )
            applied.append(path.name)
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    applied = migrate(get_settings().database_url.get_secret_value(), args.directory)
    print("Applied: " + ", ".join(applied) if applied else "Database is up to date.")


if __name__ == "__main__":
    main()
