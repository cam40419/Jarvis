"""Run pytest against a fresh, loopback-only PostgreSQL cluster, then stop it.

Supply an existing PostgreSQL bin directory with pgcrypto and pgvector installed.
This runner downloads nothing and never reads Simon configuration or an existing database.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DATABASE = "simon_acceptance_test"
USER = "simon_test_admin"


def work_directory() -> tuple[Path, str]:
    root = ROOT.resolve()
    parent = (root / ".local" / "postgres-tests").resolve()
    if not parent.is_relative_to(root):
        raise ValueError("PostgreSQL test output must remain inside the workspace")
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    work = parent / token
    work.mkdir(mode=0o700)
    (work / ".owner").write_text(token, encoding="ascii")
    return work, token


def owned_data(work: Path, token: str) -> Path:
    parent = (ROOT.resolve() / ".local" / "postgres-tests").resolve()
    resolved = work.resolve()
    if (
        not parent.is_relative_to(ROOT.resolve())
        or resolved.parent != parent
        or resolved.name != token
        or (resolved / ".owner").read_text(encoding="ascii") != token
    ):
        raise ValueError("Refusing to operate on an unowned PostgreSQL cluster")
    data = resolved / "data"
    if data.resolve() != data:
        raise ValueError("PostgreSQL test data cannot redirect to another directory")
    return data


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def command(
    args: list[str], *, env: dict[str, str], log: Path, timeout: int, check: bool = True
) -> int:
    with log.open("ab") as output:
        result = subprocess.run(
            args,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
    if check and result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed; see {log}")
    return result.returncode


def stop_test_process_tree(process: subprocess.Popen[bytes], *, env: dict[str, str]) -> None:
    """Stop only the pytest process we spawned and its descendants."""
    if os.name == "nt":
        failure: Exception | None = None
        if process.poll() is None:
            try:
                result = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=10,
                    check=False,
                )
                if result.returncode:
                    failure = RuntimeError("Owned pytest process-tree termination failed")
            except (OSError, subprocess.TimeoutExpired) as error:
                failure = error
        # Always reap our direct child, including when tree termination failed.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        if failure is not None:
            raise RuntimeError("Could not stop the complete owned pytest process tree") from failure
        return

    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2)
    # The parent can exit before a stubborn grandchild. Kill the owned group too.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def run_pytest(args: list[str], *, env: dict[str, str], log: Path, timeout: int) -> int:
    """Own pytest's process tree without capturing PostgreSQL's server process."""
    with log.open("ab") as output:
        process = subprocess.Popen(
            args,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        try:
            return process.wait(timeout=timeout)
        except BaseException:
            stop_test_process_tree(process, env=env)
            raise


@contextmanager
def isolated_postgres_environment() -> Iterator[None]:
    """Prevent libpq in this single-threaded runner from inheriting connection settings."""
    original = {key: value for key, value in os.environ.items() if key.startswith("PG")}
    try:
        for key in original:
            del os.environ[key]
        yield
    finally:
        for key in tuple(os.environ):
            if key.startswith("PG"):
                del os.environ[key]
        os.environ.update(original)


def prepare_database(port: int, password: str) -> tuple[str, dict[str, object]]:
    import psycopg
    from psycopg.conninfo import make_conninfo

    options = {
        "host": "127.0.0.1",
        "port": port,
        "user": USER,
        "password": password,
        "connect_timeout": 5,
        "options": "",
    }
    with isolated_postgres_environment():
        with psycopg.connect(dbname="postgres", autocommit=True, **options) as connection:
            connection.execute(f"CREATE DATABASE {DATABASE}")
        with psycopg.connect(dbname=DATABASE, autocommit=True, **options) as connection:
            connection.execute("CREATE EXTENSION pgcrypto")
            connection.execute("CREATE EXTENSION vector")
            version = connection.execute("SELECT version()").fetchone()[0]
            extensions = dict(
                connection.execute(
                    "SELECT extname,extversion FROM pg_extension "
                    "WHERE extname IN ('pgcrypto','vector') ORDER BY extname"
                )
            )
    return make_conninfo(dbname=DATABASE, **options), {
        "server_version": version,
        "extensions": extensions,
    }


def run(bin_dir: Path, pytest_args: list[str], *, test_timeout: int = 1800) -> int:
    if test_timeout < 1:
        raise ValueError("The pytest timeout must be positive")
    bin_dir = bin_dir.resolve(strict=True)
    suffix = ".exe" if os.name == "nt" else ""
    initdb, pg_ctl = (bin_dir / (name + suffix) for name in ("initdb", "pg_ctl"))
    if not initdb.is_file() or not pg_ctl.is_file():
        raise ValueError("The bin directory must contain initdb and pg_ctl")
    work, token = work_directory()
    data = owned_data(work, token)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["PSModuleAnalysisCachePath"] = str(work / "ModuleAnalysisCache")
    password = secrets.token_urlsafe(32)
    password_file = work / "password.txt"
    port = free_port()
    receipt: dict[str, object] = {
        "host": "127.0.0.1",
        "port": port,
        "database": DATABASE,
        "cluster_state": "not_started",
    }
    attempted_start = False
    exit_code = 1
    print(f"Disposable PostgreSQL diagnostics: {work}", flush=True)
    try:
        with password_file.open("x", encoding="ascii") as output:
            output.write(password + "\n")
        try:
            command(
                [
                    str(initdb),
                    "-D",
                    str(data),
                    "-U",
                    USER,
                    "--auth=scram-sha-256",
                    "--encoding=UTF8",
                    "--locale=C",
                    "--pwfile",
                    str(password_file),
                ],
                env=env,
                log=work / "initdb.log",
                timeout=90,
            )
        finally:
            password_file.unlink(missing_ok=True)
        owned_data(work, token)
        attempted_start = True
        command(
            [
                str(pg_ctl),
                "-D",
                str(data),
                "-l",
                str(work / "server.log"),
                "-o",
                f"-h 127.0.0.1 -p {port}",
                "-t",
                "30",
                "-w",
                "start",
            ],
            env=env,
            log=work / "lifecycle.log",
            timeout=45,
        )
        receipt["cluster_state"] = "running"
        database_url, server = prepare_database(port, password)
        receipt.update(server)
        print(json.dumps(server), flush=True)
        env["SIMON_TEST_DATABASE_URL"] = database_url
        exit_code = run_pytest(
            [sys.executable, "-m", "pytest", *pytest_args],
            env=env,
            log=work / "pytest.log",
            timeout=test_timeout,
        )
        receipt["pytest_exit_code"] = exit_code
    except KeyboardInterrupt:
        exit_code = 130
        receipt["error"] = "interrupted"
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        receipt["error"] = type(error).__name__
        print(
            f"PostgreSQL test setup or execution failed ({type(error).__name__}).", file=sys.stderr
        )
    finally:
        try:
            password_file.unlink(missing_ok=True)
        except OSError as error:
            # Password cleanup must never prevent stopping our running server.
            receipt["password_cleanup_error"] = type(error).__name__
            exit_code = exit_code or 1
        if attempted_start:
            try:
                data = owned_data(work, token)
                # Native Windows can need more than 30 seconds to flush the files
                # created by the complete migration/contract suite.
                stopped = command(
                    [str(pg_ctl), "-D", str(data), "-m", "fast", "-t", "90", "-w", "stop"],
                    env=env,
                    log=work / "lifecycle.log",
                    timeout=105,
                    check=False,
                )
                if stopped and (data / "postmaster.pid").exists():
                    raise RuntimeError("The disposable PostgreSQL cluster did not stop")
                receipt["cluster_state"] = "stopped"
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                receipt["cluster_state"] = "stop_failed"
                receipt["stop_error"] = type(error).__name__
                exit_code = exit_code or 1
                print(f"Could not stop the disposable cluster in {data}.", file=sys.stderr)
        receipt["exit_code"] = exit_code
        (work / "result.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        print(
            f"Test runner exited {exit_code}; cluster {receipt['cluster_state']}. "
            f"Pytest output: {work / 'pytest.log'}",
            flush=True,
        )
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, required=True)
    parser.add_argument("--test-timeout", type=int, default=1800)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    pytest_args = args.pytest_args
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    return run(
        args.bin_dir,
        pytest_args or ["-q", "-m", "postgres and not browser and not live"],
        test_timeout=args.test_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
