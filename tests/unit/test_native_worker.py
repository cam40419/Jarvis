"""Outbound worker never redispatches an uncertain step or exposes its bearer."""

import json
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import httpx
import pytest

from simon import native_worker
from simon.native_worker import NativeWorker, WorkerError, WorkerLease, main, server_url

TOKEN = "simon_runner_" + "s" * 43


def lease_document():
    return {
        "run_id": str(uuid4()),
        "fence": 1,
        "lease_token": "private-lease-token",
        "lease_until": (datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
        "project_id": str(uuid4()),
        "task_title": "Prepare review candidate",
        "agent_name": "Researcher",
    }


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:8000/",
        "http://127.0.0.1:8000",
        "http://[::1]:8000/prefix/",
        "https://simon.example/prefix/",
    ],
)
def test_server_url_allows_tls_and_loopback_with_deployment_prefix(value):
    assert server_url(value) == value.rstrip("/")


@pytest.mark.parametrize(
    "value",
    [
        "http://public.example",
        "ftp://localhost",
        "https://user:private-token@example.com",
        "https://example.com?key=private-token",
        "https://example.com#private-token",
        "https://example.com:99999",
        "https://example.com:bad",
        "https://example.com/../outside",
        "https://example.com\\outside",
        "https://example.com/ spaced",
        "https://",
    ],
)
def test_server_url_rejects_unsafe_credential_destinations_without_echoing_them(value):
    with pytest.raises(ValueError) as failure:
        server_url(value)
    assert "private-token" not in str(failure.value)


@pytest.mark.parametrize(
    "token", ["", "other-token", "simon_runner_short", TOKEN + " ", TOKEN + "é"]
)
def test_runner_token_validation_never_echoes_credential(token):
    with pytest.raises(ValueError, match="through the environment") as failure:
        NativeWorker("http://localhost:8000", token)
    assert TOKEN not in str(failure.value)


@pytest.mark.parametrize("seconds", [0, -1, 31, float("nan"), float("inf")])
def test_heartbeat_interval_is_bounded(seconds):
    with pytest.raises(ValueError, match="Heartbeat"):
        NativeWorker("http://localhost:8000", TOKEN, heartbeat_seconds=seconds)


def test_worker_follows_retained_lease_without_model_prompts_or_keys():
    lease = lease_document()
    calls = []
    results = ["running", "completed"]

    def respond(request):
        calls.append(request)
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json=lease, headers={"Set-Cookie": "human=do-not-send"})
        return httpx.Response(200, json={"id": lease["run_id"], "status": results.pop(0)})

    worker = NativeWorker(
        "http://localhost:8000/prefix", TOKEN, transport=httpx.MockTransport(respond)
    )
    try:
        assert worker.cycle() == "completed"
    finally:
        worker.close()
    assert len(calls) == 3
    assert all(request.url.path.startswith("/prefix/v2/execution-worker/") for request in calls)
    assert all(request.headers["Authorization"] == "Bearer " + TOKEN for request in calls)
    assert all("cookie" not in request.headers for request in calls)
    bodies = [json.loads(request.content) for request in calls]
    assert set(bodies[0]) == {"idempotency_key"}
    assert all(
        set(body) == {"run_id", "fence", "lease_token", "idempotency_key"} for body in bodies[1:]
    )
    assert bodies[1]["idempotency_key"] != bodies[2]["idempotency_key"]
    assert bodies[1]["lease_token"] == lease["lease_token"]
    parsed = WorkerLease.model_validate(lease)
    assert lease["lease_token"] not in repr(parsed)
    assert "lease_token" not in parsed.model_dump()
    assert calls[0].extensions["timeout"]["read"] == 20
    assert calls[1].extensions["timeout"]["read"] == 240


@pytest.mark.parametrize("status", ["waiting", "failed", "unknown", "cancelled", "stale", "queued"])
def test_nonrunning_step_stops_that_lease(status):
    lease = lease_document()
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json=lease if len(calls) == 1 else {"id": lease["run_id"], "status": status}
        )

    worker = NativeWorker("http://localhost:8000", TOKEN, transport=httpx.MockTransport(respond))
    try:
        assert worker.cycle() == status
        assert len(calls) == 2
    finally:
        worker.close()


def test_empty_claim_makes_no_step():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=b"null")

    worker = NativeWorker("http://localhost:8000", TOKEN, transport=httpx.MockTransport(respond))
    try:
        assert worker.cycle() == "idle"
        assert len(calls) == 1
    finally:
        worker.close()


@pytest.mark.parametrize(
    "kind", ["timeout", "invalid-json", "invalid-lease", "redirect", "server-error"]
)
def test_lost_claim_waits_out_possible_lease_before_a_fresh_claim(kind):
    calls = []

    def respond(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("private-detail", request=request)
        if kind == "invalid-json":
            return httpx.Response(200, text="private-detail")
        if kind == "invalid-lease":
            return httpx.Response(200, json={"lease_token": "private-token"})
        return httpx.Response(302 if kind == "redirect" else 503, text="private-detail")

    worker = NativeWorker("http://localhost:8000", TOKEN, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(WorkerError) as failure:
            worker.cycle()
        assert failure.value.uncertain
        assert "private" not in str(failure.value)
        assert worker.cooldown_until > datetime.now(UTC) + timedelta(seconds=600)
        assert worker.cycle() == "cooldown"
        assert len(calls) == 1
    finally:
        worker.close()


def test_lost_step_response_is_never_resent_even_with_same_lease():
    lease = lease_document()
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json=lease)
        raise httpx.ReadTimeout("private-provider-response", request=request)

    worker = NativeWorker("http://localhost:8000", TOKEN, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(WorkerError, match="server_connection_uncertain"):
            worker.cycle()
        assert worker.cycle() == "cooldown"
        assert len(calls) == 2
        worker.cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
        with pytest.raises(WorkerError):
            worker.cycle()
        assert len(calls) == 3 and calls[-1].url.path.endswith("/claim")
    finally:
        worker.close()


@pytest.mark.parametrize("status", [401, 403, 409, 422])
def test_authentication_and_conflicts_are_safe_nonretry_errors(status):
    worker = NativeWorker(
        "http://localhost:8000",
        TOKEN,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text="private-detail")),
    )
    try:
        with pytest.raises(WorkerError) as failure:
            worker.cycle()
        assert failure.value.fatal == (status != 409)
        assert "private-detail" not in str(failure.value)
    finally:
        worker.close()


@pytest.mark.parametrize("value", [{"status": "completed"}, {"id": "foreign", "status": "running"}])
def test_unmatched_step_response_stops_and_keeps_lease_cooldown(value):
    lease = lease_document()

    def respond(request):
        return httpx.Response(200, json=lease if request.url.path.endswith("/claim") else value)

    worker = NativeWorker("http://localhost:8000", TOKEN, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(WorkerError, match="step_response_invalid"):
            worker.cycle()
        assert worker.cooldown_until is not None
    finally:
        worker.close()


def test_heartbeat_runs_during_a_blocked_step_and_stops_after_completion():
    lease = lease_document()
    heartbeat_received = Event()
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json=lease)
        if request.url.path.endswith("/heartbeat"):
            heartbeat_received.set()
            return httpx.Response(200, json={"lease_until": lease["lease_until"]})
        assert heartbeat_received.wait(2)
        return httpx.Response(200, json={"id": lease["run_id"], "status": "completed"})

    worker = NativeWorker(
        "http://localhost:8000",
        TOKEN,
        transport=httpx.MockTransport(respond),
        heartbeat_seconds=0.01,
    )
    try:
        assert worker.cycle() == "completed"
        assert any(path.endswith("/heartbeat") for path in calls)
        assert worker.cooldown_until is None
    finally:
        worker.close()


def test_failed_heartbeat_prevents_another_step_after_current_response():
    lease = lease_document()
    heartbeat_received = Event()
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json=lease)
        if request.url.path.endswith("/heartbeat"):
            heartbeat_received.set()
            return httpx.Response(403, text="private-auth-detail")
        assert heartbeat_received.wait(2)
        # Wait until the heartbeat thread has processed its response, deterministically.
        for thread in __import__("threading").enumerate():
            if thread.name == "native-workflow-heartbeat":
                thread.join(2)
        return httpx.Response(200, json={"id": lease["run_id"], "status": "running"})

    worker = NativeWorker(
        "http://localhost:8000",
        TOKEN,
        transport=httpx.MockTransport(respond),
        heartbeat_seconds=0.01,
    )
    try:
        with pytest.raises(WorkerError, match="runner_authority_rejected"):
            worker.cycle()
        assert sum(path.endswith("/step") for path in calls) == 1
    finally:
        worker.close()


def test_stop_after_claim_does_not_start_model_step():
    stop = Event()
    stop.set()
    worker = NativeWorker(
        "http://localhost:8000",
        TOKEN,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=lease_document())),
    )
    try:
        assert worker.cycle(stop) == "stopped"
        assert worker.cooldown_until is not None
    finally:
        worker.close()


def test_cli_requires_environment_token_without_printing_secrets(monkeypatch, capsys):
    monkeypatch.delenv(native_worker.TOKEN_ENV, raising=False)
    assert main(["--server-url", "http://localhost:8000", "--once"]) == 2
    assert "environment" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--server-url", "https://user:private-secret@example.com", "--once"])
    assert "private-secret" not in capsys.readouterr().err


@pytest.mark.parametrize("seconds", ["0", "61", "nan"])
def test_cli_rejects_unbounded_polling(seconds):
    with pytest.raises(SystemExit):
        main(["--server-url", "http://localhost:8000", "--poll-seconds", seconds])


@pytest.mark.parametrize("outcome", ["idle", "completed", "uncertain", "fatal"])
def test_cli_once_closes_worker_and_reports_only_safe_status(monkeypatch, capsys, outcome):
    instances = []

    class Worker:
        def __init__(self, url, token):
            assert url == "http://localhost:8000" and token == TOKEN
            self.closed = False
            instances.append(self)

        def cycle(self, stop):
            if outcome in {"uncertain", "fatal"}:
                raise WorkerError("safe-code", uncertain=True, fatal=outcome == "fatal")
            return outcome

        def close(self):
            self.closed = True

    monkeypatch.setenv(native_worker.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(native_worker, "NativeWorker", Worker)
    result = main(["--server-url", "http://localhost:8000", "--once"])
    assert result == (1 if outcome in {"uncertain", "fatal"} else 0)
    assert instances[0].closed
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
