import json
import signal
from concurrent.futures import Future
from threading import Event, Lock, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest

from simon import agent_dispatcher as cli
from simon.config import Settings
from simon.domain.agent_platform import PlatformManifest
from simon.domain.agent_runs import AgentRun
from simon.domain.models import JobStatus


def completed_run(**updates):
    values = dict(
        id=uuid4(), plan_id=uuid4(), workspace_id=uuid4(), actor_id=uuid4(),
        status=JobStatus.SUCCEEDED, tasks=(), version=9,
    )
    values.update(updates)
    return AgentRun(**values)


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    records = SimpleNamespace(ticks=0, closed=False, recovered=[], stores=[], dispatchers=0)
    records.settings = Settings(
        _env_file=None, storage_backend="postgres", agent_execution_enabled=True,
        agent_state_dir=tmp_path / "same-host-state",
    )
    records.result = completed_run()
    monkeypatch.setattr(cli, "Settings", lambda: records.settings)
    manifest = PlatformManifest(max_parallel=3)
    monkeypatch.setattr(cli, "load_manifest", lambda path: manifest)

    class Store:
        def close(self):
            records.closed = True

    def store(settings, concurrency):
        records.stores.append((settings, concurrency))
        return Store()

    monkeypatch.setattr(cli, "_store", store)

    def platform(store, configured, *, state_dir, available_transports):
        records.state_dir = state_dir
        records.available_transports = available_transports
        return SimpleNamespace(store=store, manifest=configured, state_dir=state_dir,
                               agent_profiles=SimpleNamespace(
            capture_role=lambda *args, **kwargs: None,
            resolve_role=lambda *args, **kwargs: None,
        ))

    monkeypatch.setattr(cli, "AgentPlatformService", platform)

    class Runs:
        def __init__(self, platform, *, enabled):
            records.enabled = enabled
            self.platform = platform
            self.store = platform.store

        def recover_interrupted(self, identifier, version, *, operator_actor_id):
            records.recovered.append((identifier, version, operator_actor_id))
            return records.result

    monkeypatch.setattr(cli, "AgentRunService", Runs)
    monkeypatch.setattr(cli, "ProjectAutonomyService", lambda *args, **kwargs: SimpleNamespace(
        tick=lambda: 0,
    ))
    monkeypatch.setattr(cli, "project_board_service", lambda *args, **kwargs: SimpleNamespace(
        tick=lambda: 0,
    ))

    class Dispatcher:
        def __init__(self, runs, *, transport_factory):
            records.dispatchers += 1
            records.transport_factory = transport_factory

        def tick(self):
            records.ticks += 1
            if isinstance(records.result, Exception):
                raise records.result
            return records.result

    monkeypatch.setattr(cli, "AgentDispatcher", Dispatcher)
    return records


def test_once_executes_one_run_prints_only_summary_and_closes_store(runtime, capsys):
    previous = signal.getsignal(signal.SIGINT)
    cli.main(["--once"])
    assert runtime.ticks == 1 and runtime.closed and runtime.enabled
    assert runtime.state_dir == runtime.settings.agent_state_dir
    assert {
        "http", "environment", "native", "git", "mcp", "workspace_files",
    } <= set(runtime.available_transports)
    assert callable(runtime.transport_factory)
    assert runtime.stores == [(runtime.settings, 3)]
    assert signal.getsignal(signal.SIGINT) == previous
    assert json.loads(capsys.readouterr().out) == {
        "id": str(runtime.result.id), "status": "succeeded", "version": 9,
    }


def test_once_without_queued_work_returns_idle(runtime, capsys):
    runtime.result = None
    cli.main(["--once"])
    assert runtime.ticks == 1 and runtime.closed
    assert json.loads(capsys.readouterr().out) == {"status": "idle"}


def test_operator_recovery_is_explicit_and_never_constructs_a_dispatcher(runtime, capsys):
    identifier = uuid4()
    runtime.result = completed_run(id=identifier, status=JobStatus.NEEDS_HUMAN)
    cli.main(["--recover-run", str(identifier), "--expected-version", "8", "--worker-stopped"])
    assert runtime.recovered == [(identifier, 8, runtime.settings.account_admin_actor_id)]
    assert runtime.dispatchers == 0 and runtime.ticks == 0 and runtime.closed
    assert json.loads(capsys.readouterr().out) == {
        "id": str(identifier), "status": "needs_human", "version": 9,
    }


@pytest.mark.parametrize("arguments", [
    ["--recover-run", str(uuid4())],
    ["--recover-run", str(uuid4()), "--expected-version", "1"],
    ["--recover-run", str(uuid4()), "--expected-version", "0", "--worker-stopped"],
    ["--worker-stopped"], ["--expected-version", "1"],
    ["--once", "--recover-run", str(uuid4())],
    ["--poll-seconds", "0.1"], ["--poll-seconds", "11"], ["--poll-seconds", "nan"],
    ["--once", "--stop-file", "unused.request"],
])
def test_invalid_or_unacknowledged_options_never_open_store(runtime, arguments):
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2 and not runtime.stores


@pytest.mark.parametrize("changes", [
    {"storage_backend": "memory"}, {"agent_execution_enabled": False},
])
def test_dispatcher_requires_postgres_and_explicit_execution_enablement(runtime, changes):
    runtime.settings = runtime.settings.model_copy(update=changes)
    with pytest.raises(SystemExit) as error:
        cli.main(["--once"])
    assert error.value.code == 2 and not runtime.stores


def test_help_exits_before_reading_settings_or_opening_store(monkeypatch, capsys):
    def forbidden():
        pytest.fail("Help must not read runtime configuration")

    monkeypatch.setattr(cli, "Settings", forbidden)
    with pytest.raises(SystemExit) as error:
        cli.main(["--help"])
    assert error.value.code == 0
    assert "--recover-run" in capsys.readouterr().out


def test_worker_exception_is_redacted_and_store_closes(runtime, capsys):
    runtime.result = RuntimeError("postgresql://private-secret@private-host")
    with pytest.raises(SystemExit) as error:
        cli.main(["--once"])
    assert error.value.code == 1 and runtime.closed
    assert "private-secret" not in capsys.readouterr().err


def test_continuous_start_preserves_stop_requested_during_setup(runtime, monkeypatch, tmp_path):
    stop_file = tmp_path / "stale.request"
    stop_file.touch()

    def serve(dispatcher, **options):
        assert options["stop_file"] == stop_file
        assert stop_file.exists()
        options["stop"].set()

    monkeypatch.setattr(cli, "_serve", serve)
    cli.main(["--stop-file", str(stop_file)])
    assert runtime.closed


def test_console_signals_request_drain_and_restore_handlers():
    stop = Event()
    numbers = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        numbers.append(signal.SIGBREAK)
    previous = {number: signal.getsignal(number) for number in numbers}
    with cli._shutdown_signals(stop):
        for number in numbers:
            handler = signal.getsignal(number)
            assert callable(handler)
            handler(number, None)
            assert stop.is_set()
            stop.clear()
    assert {number: signal.getsignal(number) for number in numbers} == previous


def test_idle_service_probes_once_per_interval_without_busy_spinning(monkeypatch):
    records = SimpleNamespace(ticks=0, waits=[], shutdown=None)

    class Stop:
        def is_set(self):
            return len(records.waits) == 4

        def wait(self, seconds):
            records.waits.append(seconds)

    class Executor:
        def __init__(self, *, max_workers, thread_name_prefix):
            assert max_workers == 8

        def submit(self, function):
            future = Future()
            future.set_result(function())
            return future

        def shutdown(self, *, wait, cancel_futures):
            records.shutdown = (wait, cancel_futures)

    class Dispatcher:
        def tick(self):
            records.ticks += 1
            return None

    monkeypatch.setattr(cli, "ThreadPoolExecutor", Executor)
    cli._serve(Dispatcher(), concurrency=8, poll_seconds=0.2, stop=Stop())
    assert records.ticks == 4 and records.waits == [0.2] * 4
    assert records.shutdown == (True, True)


def test_service_caps_parallel_ticks_and_drains_running_work_on_shutdown():
    stop, release, all_running = Event(), Event(), Event()
    lock = Lock()
    records = SimpleNamespace(active=0, peak=0, ticks=0)
    errors = []

    class Dispatcher:
        def tick(self):
            with lock:
                records.active += 1
                records.ticks += 1
                records.peak = max(records.peak, records.active)
                if records.active == 3:
                    all_running.set()
            try:
                assert release.wait(5), "Test worker was not released"
            finally:
                with lock:
                    records.active -= 1
            return None

    def serve():
        try:
            cli._serve(Dispatcher(), concurrency=3, poll_seconds=0.2, stop=stop)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=serve)
    thread.start()
    try:
        assert all_running.wait(5)
        stop.set()
        # Shutdown waits for in-flight work rather than abandoning worker threads.
        assert thread.is_alive()
    finally:
        stop.set()
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert records.peak == 3 and records.ticks == 3 and records.active == 0


def test_stop_request_drains_active_tick_without_claiming_more_work(tmp_path):
    stop_file = tmp_path / "stop.request"
    stop, release, started = Event(), Event(), Event()
    calls = []

    class Dispatcher:
        def tick(self):
            calls.append(True)
            started.set()
            assert release.wait(5)
            return None

    thread = Thread(target=cli._serve, args=(Dispatcher(),), kwargs={
        "concurrency": 1, "poll_seconds": 0.05, "stop": stop, "stop_file": stop_file,
    })
    thread.start()
    try:
        assert started.wait(5)
        stop_file.touch()
        assert stop.wait(5)
        assert thread.is_alive() and stop_file.exists()
    finally:
        stop.set()
        release.set()
        thread.join(5)
    assert not thread.is_alive() and len(calls) == 1


def test_preexisting_stop_request_never_claims_a_run(tmp_path):
    stop_file = tmp_path / "existing.request"
    stop_file.touch()
    stop = Event()

    class Dispatcher:
        def tick(self):
            pytest.fail("Existing stop requests must be honored before dispatch")

    cli._serve(Dispatcher(), concurrency=1, poll_seconds=0.2, stop=stop, stop_file=stop_file)
    assert stop.is_set() and stop_file.exists()


def test_slow_board_poll_does_not_block_other_runs_and_is_drained():
    stop, release, coordinating, dispatched = Event(), Event(), Event(), Event()
    coordination_calls = []

    def project_tick():
        coordination_calls.append(True)
        coordinating.set()
        assert release.wait(5)
        return 0

    class Dispatcher:
        def tick(self):
            dispatched.set()
            return None

    thread = Thread(target=cli._serve, args=(Dispatcher(),), kwargs={
        "concurrency": 1, "poll_seconds": 0.05, "stop": stop,
        "project_tick": project_tick,
    })
    thread.start()
    try:
        assert coordinating.wait(5) and dispatched.wait(5)
        stop.set()
        assert thread.is_alive()
    finally:
        stop.set()
        release.set()
        thread.join(5)
    assert not thread.is_alive() and len(coordination_calls) == 1


def test_dispatcher_writes_failure_to_rotating_log_without_secrets(runtime, tmp_path):
    runtime.result = RuntimeError("postgresql://private-secret@private-host")
    log = tmp_path / "dispatcher.log"
    with pytest.raises(SystemExit) as failure:
        cli.main(["--once", "--log-file", str(log)])
    assert failure.value.code == 1 and runtime.closed
    text = log.read_text()
    assert "Agent dispatcher failed (RuntimeError)" in text
    assert "private-secret" not in text
    assert not cli.logger.handlers
