"""Exercise real idle workers against a disposable PostgreSQL schema, without providers."""

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(("module", "started"), [
    ("simon.assistant_worker", "Simon assistant worker started"),
    ("simon.agent_dispatcher", "Simon agent dispatcher started"),
])
def test_idle_worker_starts_drains_and_restarts(postgres_url, tmp_path, module, started):
    import psycopg

    manifest = tmp_path / "manifest.json"
    manifest.write_text('{}', encoding="utf-8")
    environment = os.environ | {
        "SIMON_STORAGE_BACKEND": "postgres", "SIMON_DATABASE_URL": postgres_url,
        "SIMON_MODEL_PROVIDER": "openai", "SIMON_OPENAI_API_KEY": "unused-synthetic-key",
        "SIMON_AGENT_EXECUTION_ENABLED": "true", "SIMON_AGENT_MANIFEST_FILE": str(manifest),
        "SIMON_AGENT_STATE_DIR": str(tmp_path / "agents"),
        "SIMON_LOCAL_FILES_DIR": str(tmp_path / "files"),
        "SIMON_LOCAL_FILES_ENABLED": "true",
    }
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    stop = tmp_path / "stop.request"
    for attempt in range(2):
        stop.unlink(missing_ok=True)
        log = tmp_path / f"worker-{attempt}.log"
        process = subprocess.Popen(
            [sys.executable, "-m", module, "--stop-file", str(stop),
             "--log-file", str(log), "--poll-seconds", "0.2"],
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                assert process.poll() is None, "Worker exited before becoming ready"
                if log.exists() and started in log.read_text(encoding="utf-8"):
                    break
                time.sleep(0.05)
            else:
                pytest.fail("Worker did not become ready")
            # Allow idle polling to use actual store connections, then request a drain.
            time.sleep(0.4)
            assert process.poll() is None
            stop.touch()
            assert process.wait(timeout=10) == 0
            assert stop.exists()
            contents = log.read_text(encoding="utf-8")
            assert "stopped" in contents and "ERROR" not in contents
            assert "unused-synthetic-key" not in contents
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    with psycopg.connect(postgres_url) as connection:
        assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
