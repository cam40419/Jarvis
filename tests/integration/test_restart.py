import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.postgres


@contextmanager
def running_api(
    database_url: str,
    log_path: Path,
    *,
    dev_login=True,
    hostname="127.0.0.1",
    model_provider="local",
):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    environment = os.environ | {
        "JARVIS_ENVIRONMENT": "development",
        "JARVIS_STORAGE_BACKEND": "postgres",
        "JARVIS_DATABASE_URL": database_url,
        "JARVIS_PUBLIC_ORIGIN": f"http://{hostname}:{port}",
        "JARVIS_RP_ID": hostname,
        "JARVIS_DEV_LOGIN_ENABLED": "true" if dev_login else "false",
        "JARVIS_DEV_LOGIN_TOKEN": "process-development-secret-32-characters",
        "JARVIS_MODEL_PROVIDER": model_provider,
    }
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "jarvis.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=environment,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            with httpx.Client(base_url=f"http://{hostname}:{port}", trust_env=False) as client:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        pytest.fail("API exited during startup; inspect " + str(log_path))
                    try:
                        if client.get("/health/live").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail("API startup timed out")
                yield client
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_data_survives_api_process_restart(postgres_url: str, tmp_path: Path) -> None:
    from jarvis.seed import seed_development_identity

    seed_development_identity(postgres_url)
    body = {
        "kind": "test.job",
        "input": {"message": "survives restart"},
        "idempotency_key": "process-restart-001",
    }
    with running_api(postgres_url, tmp_path / "first.log") as client:
        origin = str(client.base_url).rstrip("/")
        login = client.post(
            "/auth/dev-login",
            headers={"Origin": origin},
            json={"token": "process-development-secret-32-characters"},
        )
        assert login.status_code == 200
        cookies = dict(client.cookies)
        headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        response = client.post("/v1/jobs", headers=headers, json=body)
        assert response.status_code == 202
        original = response.json()
        thread = client.post(
            "/v1/threads",
            headers=headers,
            json={
                "title": "Restart test",
                "idempotency_key": "restart-thread-001",
            },
        ).json()
        run_body = {"text": "Persist this turn", "idempotency_key": "restart-run-001"}
        memory = client.post(
            "/v1/memories",
            headers=headers,
            json={
                "subject": "Restart preference",
                "content": "Keep this fact",
                "idempotency_key": "restart-memory-001",
            },
        ).json()
        run_path = f"/v1/threads/{thread['id']}/runs"
        run = client.post(run_path, headers=headers, json=run_body).json()
    with running_api(postgres_url, tmp_path / "second.log") as client:
        client.cookies.update(cookies)
        headers["Origin"] = str(client.base_url).rstrip("/")
        response = client.get(f"/v1/jobs/{original['id']}", headers=headers)
        assert response.status_code == 200
        assert response.json() == original
        assert client.post("/v1/jobs", headers=headers, json=body).json() == original
        assert client.get(f"/v1/runs/{run['id']}").json() == run
        assert client.get("/v1/memories").json() == [memory]
        assert run["memory_context"][0]["source_memory_id"] == memory["id"]
        assert client.post(run_path, headers=headers, json=run_body).json() == run
        assert len(client.get(f"/v1/threads/{thread['id']}/messages").json()) == 2
        resumed = client.get(f"/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "2"})
        assert resumed.status_code == 200
        assert "id: 2\n" not in resumed.text and "id: 4\n" in resumed.text
