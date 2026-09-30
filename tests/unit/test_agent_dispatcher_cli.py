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

    def platform(store, configured, *, state_dir):
        records.state_dir = state_dir
        return SimpleNamespace(store=store, manifest=configured)

    monkeypatch.setattr(cli, "AgentPlatformService", platform)

    class Runs:
        def __init__(self, platform, *, enabled):
            records.enabled = enabled
            self.platform = platform

        def recover_interrupted(self, identifier, version, *, operator_actor_id):
            records.recovered.append((identifier, version, operator_actor_id))
            return records.result

    monkeypatch.setattr(cli, "AgentRunService", Runs)

    class Dispatcher:
        def __init__(self, runs):
            records.dispatchers += 1

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
