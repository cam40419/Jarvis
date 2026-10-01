from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from simon import worker_startup as startup


@pytest.fixture
def runtime(monkeypatch):
    state = SimpleNamespace(now=0.0, attempts=0, migrations=0, failures=[], sleeps=[])

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            assert query in ("SELECT 1", "SET statement_timeout = '2s'")

    def connect(url, *, connect_timeout):
        assert url == "synthetic-private-database" and connect_timeout == 2
        state.attempts += 1
        if state.failures:
            raise state.failures.pop(0)
        return Connection()

    def migrate(url, *, lock_timeout_ms, statement_timeout_ms):
        assert lock_timeout_ms == 5000 and statement_timeout_ms == 30000
        state.migrations += 1
        return []

    def sleep(seconds):
        state.sleeps.append(seconds)
        state.now += seconds

    monkeypatch.setattr(startup.psycopg, "connect", connect)
    monkeypatch.setattr(startup, "migrate", migrate)
    monkeypatch.setattr(startup.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(startup.time, "sleep", sleep)
    return state


def test_cold_database_retries_then_migrates_once(runtime, caplog):
    caplog.set_level("INFO")
    runtime.failures = [psycopg.OperationalError("private-socket-address") for _ in range(2)]
    assert startup.prepare("synthetic-private-database", timeout=20) == 0
    assert runtime.attempts == 3 and runtime.migrations == 1
    assert runtime.sleeps == [2, 2]
    assert "Waiting for" in caplog.text and "Database ready" in caplog.text
    assert "private-socket-address" not in caplog.text


def test_timeout_leaves_database_unmigrated_and_redacts_diagnostics(runtime, caplog):
    runtime.failures = [psycopg.OperationalError("private-secret") for _ in range(10)]
    assert startup.prepare("synthetic-private-database", timeout=3) == 1
    assert runtime.migrations == 0 and runtime.sleeps == [2, 1]
    assert "timed out" in caplog.text and "private-secret" not in caplog.text


@pytest.mark.parametrize("marker_name", ["stop_file", "maintenance_file"])
def test_operator_marker_stops_wait_without_opening_database(runtime, tmp_path, marker_name):
    marker = tmp_path / "stop.request"
    marker.touch()
    assert startup.prepare("synthetic-private-database", **{marker_name: marker}) == startup.STOPPED
    assert runtime.attempts == 0 and runtime.migrations == 0 and marker.exists()


def test_stop_during_wait_prevents_migration(runtime, tmp_path, monkeypatch):
    marker = tmp_path / "stop.request"
    runtime.failures = [psycopg.OperationalError("offline")]
    monkeypatch.setattr(startup.time, "sleep", lambda seconds: marker.touch())
    assert startup.prepare("synthetic-private-database", stop_file=marker) == startup.STOPPED
    assert runtime.attempts == 1 and runtime.migrations == 0


def test_bad_credentials_fail_without_repeated_attempts(runtime, caplog):
    class CredentialsError(psycopg.OperationalError):
        sqlstate = "28P01"

    runtime.failures = [CredentialsError("private-password")]
    assert startup.prepare("synthetic-private-database") == 1
    assert runtime.attempts == 1 and runtime.migrations == 0 and not runtime.sleeps
    assert "28P01" in caplog.text and "private-password" not in caplog.text


def test_migration_failure_is_not_retried_or_exposed(runtime, monkeypatch, caplog):
    def migrate(url, **timeouts):
        assert timeouts == {"lock_timeout_ms": 5000, "statement_timeout_ms": 30000}
        runtime.migrations += 1
        raise ValueError("private migration content")

    monkeypatch.setattr(startup, "migrate", migrate)
    assert startup.prepare("synthetic-private-database") == 1
    assert runtime.migrations == 1 and not runtime.sleeps
    assert "migration failed (ValueError)" in caplog.text
    assert "private migration content" not in caplog.text


def test_stop_after_connection_prevents_migration(runtime, monkeypatch, tmp_path):
    marker = tmp_path / "stop.request"
    connect = startup.psycopg.connect

    def stopped(*args, **kwargs):
        connection = connect(*args, **kwargs)
        marker.touch()
        return connection

    monkeypatch.setattr(startup.psycopg, "connect", stopped)
    assert startup.prepare("synthetic-private-database", stop_file=marker) == startup.STOPPED
    assert runtime.migrations == 0


def test_stop_during_migration_failure_preserves_marker_without_restart(
    runtime, monkeypatch, tmp_path, caplog,
):
    marker = tmp_path / "stop.request"

    def migrate(url, **timeouts):
        runtime.migrations += 1
        marker.touch()
        raise psycopg.errors.LockNotAvailable("private lock diagnostic")

    monkeypatch.setattr(startup, "migrate", migrate)
    caplog.set_level("INFO")
    assert startup.prepare("synthetic-private-database", stop_file=marker) == startup.STOPPED
    assert marker.exists() and runtime.migrations == 1 and not runtime.sleeps
    assert "LockNotAvailable" in caplog.text and "private lock diagnostic" not in caplog.text
    assert "stopped during migrations" in caplog.text


@pytest.mark.parametrize("timeouts", [
    {"lock_timeout_ms": 0}, {"lock_timeout_ms": True}, {"statement_timeout_ms": 600001},
    {"statement_timeout_ms": -1}, {"statement_timeout_ms": 1.5},
])
def test_invalid_migration_limits_rejected_before_connection(runtime, timeouts):
    from simon.migrate import migrate

    with pytest.raises(ValueError, match="timeouts"):
        migrate("synthetic-private-database", **timeouts)
    assert runtime.attempts == 0


def test_cli_writes_private_safe_startup_log_and_closes_it(runtime, monkeypatch, tmp_path):
    from pydantic import SecretStr

    monkeypatch.setattr(startup, "Settings", lambda: SimpleNamespace(
        storage_backend="postgres", database_url=SecretStr("synthetic-private-database"),
    ))
    log: Path = tmp_path / "startup.log"
    startup.logger.setLevel("INFO")
    assert startup.main(["--log-file", str(log)]) == 0
    assert "Database ready" in log.read_text() and "synthetic-private" not in log.read_text()
    assert not startup.logger.handlers


@pytest.mark.parametrize("arguments", [["--timeout", "nan"], ["--timeout", "0"],
                                        ["--poll-seconds", "inf"]])
def test_invalid_wait_configuration_does_not_connect(runtime, arguments):
    with pytest.raises(SystemExit) as failure:
        startup.main(arguments)
    assert failure.value.code == 2 and runtime.attempts == 0
