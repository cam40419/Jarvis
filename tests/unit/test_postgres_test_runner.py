"""The disposable database runner never adopts an existing cluster or leaks its password."""

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "test_postgres_runner", Path(__file__).resolve().parents[2] / "scripts/test_postgres.py"
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "free_port", lambda: 55439)
    monkeypatch.setenv("PGHOST", "operator-database.invalid")
    monkeypatch.setenv("SIMON_TEST_DATABASE_URL", "operator-value-must-be-replaced")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("initdb", "pg_ctl"):
        (binaries / (name + (".exe" if os.name == "nt" else ""))).touch()
    h = SimpleNamespace(root=tmp_path, bin=binaries, calls=[], failure=None, password=None)

    def database(port, password):
        assert port == 55439 and password == h.password
        if h.failure == "database":
            raise RuntimeError("Synthetic database setup failure")
        return "host=127.0.0.1 dbname=simon_acceptance_test password=" + password, {
            "server_version": "Synthetic PostgreSQL",
            "extensions": {"pgcrypto": "1.3", "vector": "0.8.6"},
        }

    def command(args, **kwargs):
        h.calls.append((args, dict(kwargs["env"])))
        assert kwargs["cwd"] == tmp_path
        assert kwargs["timeout"] > 0
        assert "PGHOST" not in kwargs["env"]
        result = 0
        if Path(args[0]).stem == "initdb":
            h.password = Path(args[args.index("--pwfile") + 1]).read_text().strip()
            data = Path(args[args.index("-D") + 1])
            data.mkdir()
            (data / "PG_VERSION").write_text("16")
            assert "--auth=scram-sha-256" in args
            result = int(h.failure == "init")
        elif args[-1] == "start":
            data = Path(args[args.index("-D") + 1])
            (data / "postmaster.pid").write_text("synthetic")
            assert args[args.index("-o") + 1] == "-h 127.0.0.1 -p 55439"
            assert not (data.parent / "password.txt").exists()
            result = int(h.failure == "start")
        elif args[-1] == "stop":
            data = Path(args[args.index("-D") + 1])
            assert data.parent.name == (data.parent / ".owner").read_text()
            result = int(h.failure == "stop")
            if not result:
                (data / "postmaster.pid").unlink(missing_ok=True)
        else:
            assert args[1:3] == ["-m", "pytest"]
            assert h.password in kwargs["env"]["SIMON_TEST_DATABASE_URL"]
            assert kwargs["env"]["PATH"].startswith(str(binaries) + os.pathsep)
            assert Path(kwargs["env"]["PSModuleAnalysisCachePath"]).is_relative_to(tmp_path)
            if h.failure == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            if h.failure == "interrupt":
                raise KeyboardInterrupt
            result = 5 if h.failure == "pytest" else 0
        return subprocess.CompletedProcess(args, result)

    monkeypatch.setattr(runner, "prepare_database", database)
    monkeypatch.setattr(runner.subprocess, "run", command)

    def fake_pytest(args, **kwargs):
        return runner.command(args, **kwargs, check=False)

    monkeypatch.setattr(runner, "run_pytest", fake_pytest)
    return h


@pytest.mark.parametrize(
    "failure,expected,stopped",
    [
        (None, 0, True),
        ("init", 1, False),
        ("start", 1, True),
        ("database", 1, True),
        ("pytest", 5, True),
        ("timeout", 1, True),
        ("interrupt", 130, True),
        ("stop", 1, True),
    ],
)
def test_cleanup_and_test_exit_codes(harness, failure, expected, stopped):
    h = harness
    h.failure = failure
    assert runner.run(h.bin, ["-q", "-m", "postgres"]) == expected
    work = next((h.root / ".local/postgres-tests").iterdir())
    assert not (work / "password.txt").exists()
    stop_calls = [args for args, _env in h.calls if args[-1] == "stop"]
    assert len(stop_calls) == int(stopped)
    receipt_text = (work / "result.json").read_text()
    assert h.password not in receipt_text
    assert "operator-value" not in receipt_text
    receipt = json.loads(receipt_text)
    assert receipt["exit_code"] == expected
    assert receipt["cluster_state"] == (
        "stop_failed" if failure == "stop" else "stopped" if stopped else "not_started"
    )


def test_repeated_invocations_use_separate_clusters(harness):
    assert runner.run(harness.bin, ["-q"]) == 0
    assert runner.run(harness.bin, ["-q"]) == 0
    work = list((harness.root / ".local/postgres-tests").iterdir())
    assert len(work) == 2 and work[0] != work[1]


def test_existing_directory_and_modified_ownership_are_rejected(harness, monkeypatch):
    work, token = runner.work_directory()
    monkeypatch.setattr(runner, "uuid4", lambda: SimpleNamespace(hex=token))
    with pytest.raises(FileExistsError):
        runner.work_directory()
    with pytest.raises(ValueError, match="unowned"):
        runner.owned_data(harness.root, token)
    (work / ".owner").write_text("someone-elses-cluster")
    with pytest.raises(ValueError, match="unowned"):
        runner.owned_data(work, token)
    assert harness.calls == []


def test_workspace_escape_is_rejected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    monkeypatch.setattr(runner, "ROOT", workspace)
    try:
        (workspace / ".local").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("The host does not permit directory symlinks")
    with pytest.raises(ValueError, match="inside the workspace"):
        runner.work_directory()
    assert list(external.iterdir()) == []


def test_real_test_process_exit_status_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    assert (
        runner.run_pytest(
            [sys.executable, "-c", "raise SystemExit(7)"],
            env=dict(os.environ),
            log=tmp_path / "pytest.log",
            timeout=10,
        )
        == 7
    )


def test_timeout_stops_owned_parent_and_listening_grandchild(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    marker = tmp_path / "grandchild.json"
    child_code = textwrap.dedent(
        """
        import json, os, socket, sys, time
        from pathlib import Path
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            Path(sys.argv[1]).write_text(json.dumps({
                "pid": os.getpid(), "port": listener.getsockname()[1]
            }))
            while True:
                time.sleep(1)
        """
    )
    parent_code = (
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}, sys.argv[1]], "
        "creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)\n"
        "time.sleep(60)\n"
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            runner.run_pytest(
                [sys.executable, "-c", parent_code, str(marker)],
                env=dict(os.environ),
                log=tmp_path / "pytest.log",
                timeout=5,
            )
        assert marker.exists(), "Synthetic grandchild did not start before the test timeout"
        child = json.loads(marker.read_text())
        # The child never closes this listener while alive. A refused connection proves exit.
        with socket.socket() as probe:
            probe.settimeout(1)
            assert probe.connect_ex(("127.0.0.1", child["port"])) != 0
    finally:
        # Keep a failed cleanup regression from leaving its own synthetic child behind.
        if marker.exists():
            child = json.loads(marker.read_text())
            with socket.socket() as probe:
                probe.settimeout(1)
                if probe.connect_ex(("127.0.0.1", child["port"])) == 0:
                    os.kill(child["pid"], signal.SIGTERM)


@pytest.mark.parametrize("connection_fails", [False, True])
def test_prepare_database_clears_and_restores_parent_libpq_environment(
    monkeypatch, connection_fails
):
    psycopg = pytest.importorskip("psycopg")
    inherited = {"PGHOSTADDR": "192.0.2.1", "PGSERVICE": "operator", "PGSSLMODE": "verify-full"}
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("RUNNER_UNRELATED_SETTING", "preserve-me")
    original = dict(os.environ)
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement):
            if statement == "SELECT version()":
                return SimpleNamespace(fetchone=lambda: ("Synthetic PostgreSQL",))
            if "pg_extension" in statement:
                return [("pgcrypto", "1.3"), ("vector", "0.8.6")]
            return None

    def connect(**kwargs):
        assert not any(key.startswith("PG") for key in os.environ)
        assert os.environ["RUNNER_UNRELATED_SETTING"] == "preserve-me"
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 55439
        assert kwargs["options"] == ""
        calls.append(kwargs)
        if connection_fails:
            raise psycopg.OperationalError("Synthetic setup failure")
        return Connection()

    monkeypatch.setattr(psycopg, "connect", connect)
    if connection_fails:
        with pytest.raises(psycopg.OperationalError, match="Synthetic setup failure"):
            runner.prepare_database(55439, "synthetic-password")
    else:
        _, versions = runner.prepare_database(55439, "synthetic-password")
        assert versions["extensions"] == {"pgcrypto": "1.3", "vector": "0.8.6"}
        assert len(calls) == 2
    assert dict(os.environ) == original


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ([], ["-q", "-m", "postgres and not browser and not live"]),
        (["--", "-q", "-m", "postgres"], ["-q", "-m", "postgres"]),
    ],
)
def test_cli_defaults_exclude_optional_categories_and_preserve_explicit_selection(
    tmp_path, monkeypatch, arguments, expected
):
    monkeypatch.setattr(sys, "argv", ["test_postgres.py", "--bin-dir", str(tmp_path), *arguments])
    calls = []

    def run(bin_dir, pytest_args, *, test_timeout):
        calls.append((bin_dir, pytest_args, test_timeout))
        return 7

    monkeypatch.setattr(runner, "run", run)
    assert runner.main() == 7
    assert calls == [(tmp_path, expected, 1800)]
