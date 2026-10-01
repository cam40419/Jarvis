"""Real PostgreSQL lock and SQL timeouts, isolated from the live schema/database."""

import time
from threading import Timer

import psycopg
import pytest

from simon.migrate import migrate, migration_directory
from simon.worker_startup import STOPPED, prepare

pytestmark = pytest.mark.postgres


def test_worker_startup_held_advisory_lock_times_out_and_honors_stop(
    postgres_url,
    tmp_path,
    caplog,
):
    marker = tmp_path / "stop.request"
    caplog.set_level("INFO")
    with psycopg.connect(postgres_url, autocommit=True) as blocker:
        blocker.execute("SELECT pg_advisory_lock(741982001)")
        timer = Timer(0.2, marker.touch)
        timer.start()
        started = time.monotonic()
        try:
            assert prepare(postgres_url, stop_file=marker) == STOPPED
        finally:
            timer.cancel()
            timer.join(timeout=2)
            blocker.execute("SELECT pg_advisory_unlock(741982001)")
        elapsed = time.monotonic() - started
    assert 4 <= elapsed < 15
    assert marker.exists()
    assert "LockNotAvailable" in caplog.text and "stopped during migrations" in caplog.text
    assert postgres_url not in caplog.text
    # The retained stop request wins even after the lock clears; a later explicit
    # operator resume can migrate successfully, with no abandoned lock/transaction.
    assert prepare(postgres_url, stop_file=marker) == STOPPED
    marker.unlink()
    assert prepare(postgres_url, stop_file=marker) == 0


def test_statement_timeout_rolls_back_new_schema_and_version_record(postgres_url, tmp_path):
    for source in migration_directory().glob("*.sql"):
        (tmp_path / source.name).write_bytes(source.read_bytes())
    pending = tmp_path / "9999_timeout_test.sql"
    pending.write_text(
        "CREATE TABLE startup_timeout_probe (id integer); SELECT pg_sleep(10);",
        encoding="utf-8",
    )
    started = time.monotonic()
    with pytest.raises(psycopg.errors.QueryCanceled):
        migrate(postgres_url, tmp_path, lock_timeout_ms=1000, statement_timeout_ms=200)
    assert time.monotonic() - started < 5
    with psycopg.connect(postgres_url) as connection:
        relation = connection.execute("SELECT to_regclass('startup_timeout_probe')").fetchone()[0]
        assert relation is None
        assert (
            connection.execute(
                "SELECT count(*) FROM schema_migrations WHERE name=%s",
                (pending.name,),
            ).fetchone()[0]
            == 0
        )
    pending.write_text("CREATE TABLE startup_timeout_probe (id integer);", encoding="utf-8")
    assert migrate(postgres_url, tmp_path, lock_timeout_ms=1000, statement_timeout_ms=1000) == [
        pending.name
    ]
    assert migrate(postgres_url, tmp_path, lock_timeout_ms=1000, statement_timeout_ms=1000) == []
