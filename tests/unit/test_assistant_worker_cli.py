import signal
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from simon import assistant_worker as cli
from simon.config import Settings


@pytest.fixture
def runtime(monkeypatch):
    records = SimpleNamespace(opened=0, closed=False, tasks=0, sessions=0, error=None)
    records.settings = Settings(
        _env_file=None,
        storage_backend="postgres",
        model_provider="openai",
        openai_api_key=SecretStr("synthetic-worker-key"),
    )
    monkeypatch.setattr(cli, "Settings", lambda: records.settings)

    class Store:
        def close(self):
            records.closed = True

    def store(settings):
        records.opened += 1
        return Store()

    class Sessions:
        def tick(self):
            records.sessions += 1
            if records.error:
                raise records.error
            return 1

    monkeypatch.setattr(cli, "_store", store)
    monkeypatch.setattr(cli, "_services", lambda store, settings: Sessions())
    return records


def test_check_does_not_open_database_or_write_logs(runtime, tmp_path, capsys):
    log = tmp_path / "unused.log"
    cli.main(["--check", "--log-file", str(log)])
    assert runtime.opened == 0 and not log.exists()
    assert "availability were not checked" in capsys.readouterr().out


def test_once_observes_task_result_after_waiting_and_closes_store(runtime):
    cli.main(["--once"])
    assert runtime.opened == 1 and runtime.closed
    assert 1 <= runtime.sessions <= 2


def test_once_reports_async_failure_without_exposing_exception_text(runtime, caplog, capsys):
    runtime.error = RuntimeError("postgres://private-secret@host")
    with pytest.raises(SystemExit) as error:
        cli.main(["--once"])
    assert error.value.code == 1 and runtime.closed
    assert "private-secret" not in caplog.text + capsys.readouterr().err


def test_existing_stop_marker_prevents_database_connection_and_survives(runtime, tmp_path):
    marker = tmp_path / "stop.request"
    marker.touch()
    cli.main(["--stop-file", str(marker)])
    assert runtime.opened == 0 and marker.exists()


def test_initialization_failure_closes_database_and_redacts_details(runtime, monkeypatch, capsys):
    def failed(store, settings):
        raise RuntimeError("provider-private-key")

    monkeypatch.setattr(cli, "_services", failed)
    with pytest.raises(SystemExit) as error:
        cli.main(["--once"])
    assert error.value.code == 1 and runtime.closed
    assert "provider-private-key" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "changes",
    [
        {"storage_backend": "memory"},
        {"model_provider": "local"},
        {"openai_api_key": None},
    ],
)
def test_invalid_worker_configuration_never_opens_store(runtime, changes):
    runtime.settings = runtime.settings.model_copy(update=changes)
    with pytest.raises(SystemExit) as error:
        cli.main(["--check"])
    assert error.value.code == 2 and runtime.opened == 0


@pytest.mark.parametrize(
    "arguments",
    [
        ["--once", "--check"],
        ["--once", "--stop-file", "unused"],
        ["--check", "--stop-file", "unused"],
        ["--poll-seconds", "nan"],
        ["--poll-seconds", "0.1"],
        ["--poll-seconds", "11"],
    ],
)
def test_invalid_cli_arguments_never_open_store(runtime, arguments):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2 and runtime.opened == 0


def test_help_does_not_read_configuration(monkeypatch):
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("Read settings for help"))
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])
    assert error.value.code == 0


def test_signals_request_drain_and_restore_previous_handlers():
    stop = Event()
    numbers = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        numbers.append(signal.SIGBREAK)
    previous = {number: signal.getsignal(number) for number in numbers}
    with cli._shutdown_signals(stop):
        for number in numbers:
            signal.getsignal(number)(number, None)
            assert stop.is_set()
            stop.clear()
    assert {number: signal.getsignal(number) for number in numbers} == previous


def test_stop_request_drains_two_active_calls_and_keeps_marker(tmp_path):
    stop, release, running = Event(), Event(), Event()
    lock = Lock()
    calls = []
    results = []
    marker = tmp_path / "stop.request"

    class Service:
        def tick(self):
            with lock:
                calls.append(True)
                if len(calls) == 2:
                    running.set()
            assert release.wait(5)
            return 0

    def serve():
        results.append(cli._serve(Service(), poll_seconds=0.02, stop=stop, stop_file=marker))

    thread = Thread(target=serve)
    thread.start()
    try:
        assert running.wait(5)
        marker.touch()
        assert stop.wait(5) and thread.is_alive()
        assert marker.exists()
    finally:
        stop.set()
        release.set()
        thread.join(5)
    assert not thread.is_alive() and len(calls) == 2 and results == [True]


def test_rotating_file_logger_is_bounded_and_closed(runtime, tmp_path, monkeypatch):
    created = []
    original = cli.RotatingFileHandler

    def handler(*args, **kwargs):
        result = original(*args, **kwargs)
        created.append(result)
        return result

    monkeypatch.setattr(cli, "RotatingFileHandler", handler)
    cli.logger.setLevel("INFO")
    log = tmp_path / "logs" / "worker.log"
    cli.main(["--once", "--log-file", str(log)])
    assert "Simon assistant worker started" in log.read_text()
    assert created[0].maxBytes == 5 * 1024 * 1024 and created[0].backupCount == 3
    assert created[0].stream is None and created[0] not in cli.logger.handlers
