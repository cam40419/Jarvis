"""Outbound native workflow runner: python -m simon.native_worker.

The server owns prompts, provider credentials, execution and durable accounting.
This optional process claims bounded work and keeps its lease alive over HTTP.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import signal
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from math import isfinite
from threading import Event, Lock, Thread
from types import FrameType
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

TOKEN_ENV = "SIMON_EXECUTION_RUNNER_TOKEN"
_MAX_LEASE_SECONDS = 600


class WorkerError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False, fatal: bool = False) -> None:
        super().__init__(code)
        self.code, self.uncertain, self.fatal = code, uncertain, fatal


class WorkerLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    run_id: UUID
    fence: int = Field(ge=1, strict=True)
    lease_token: str = Field(min_length=1, max_length=256, repr=False, exclude=True)
    lease_until: AwareDatetime
    project_id: UUID
    task_title: str = Field(max_length=200)
    agent_name: str = Field(max_length=200)


def server_url(value: str) -> str:
    """Keep the runner credential on the exact operator-selected server origin."""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(character.isspace() or ord(character) < 32 for character in value)
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise ValueError
        _ = parsed.port
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname == "localhost"
            if not loopback:
                raise ValueError
    except ValueError:
        raise ValueError(
            "Use an HTTPS server URL, or loopback HTTP, without credentials or query."
        ) from None
    return value.rstrip("/")


class NativeWorker:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        heartbeat_seconds: float = 30,
    ) -> None:
        self.url = server_url(url) + "/v2/execution-worker"
        if (
            not token.startswith("simon_runner_")
            or not 20 <= len(token) <= 256
            or not token.isascii()
            or any(character.isspace() or ord(character) < 33 for character in token)
        ):
            raise ValueError(
                "A valid execution runner token must be supplied through the environment."
            )
        if not isfinite(heartbeat_seconds) or not 0 < heartbeat_seconds <= 30:
            raise ValueError("Heartbeat interval must be positive and at most 30 seconds.")
        self._client = httpx.Client(
            transport=transport,
            headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(240, connect=10, pool=10, write=30),
            trust_env=False,
            follow_redirects=False,
        )
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_lock = Lock()
        self.cooldown_until: datetime | None = None

    def close(self) -> None:
        self._client.close()

    def _request(self, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            request = self._client.build_request(
                "GET" if body is None else "POST",
                self.url + path,
                json=body,
                timeout=httpx.Timeout(
                    240 if path == "/step" else 20, connect=10, pool=10, write=20
                ),
            )
            # A runner never adopts a server-issued human session cookie.
            request.headers.pop("cookie", None)
            response = self._client.send(request)
        except httpx.HTTPError:
            raise WorkerError("server_connection_uncertain", uncertain=body is not None) from None
        if response.status_code in {401, 403}:
            raise WorkerError("runner_authority_rejected", fatal=True)
        if response.status_code == 409:
            raise WorkerError("lease_or_workflow_changed")
        if response.status_code != 200:
            raise WorkerError(
                "server_response_unavailable",
                uncertain=body is not None,
                fatal=400 <= response.status_code < 500,
            )
        try:
            return response.json()
        except ValueError:
            raise WorkerError("server_response_invalid", uncertain=body is not None) from None

    @staticmethod
    def _command(lease: WorkerLease) -> dict[str, Any]:
        return {
            "run_id": str(lease.run_id),
            "fence": lease.fence,
            "lease_token": lease.lease_token,
            "idempotency_key": "runner-" + str(uuid4()),
        }

    def _cooldown(self, until: datetime) -> None:
        with self._lease_lock:
            if self.cooldown_until is None or until > self.cooldown_until:
                self.cooldown_until = until

    def cycle(self, stop: Event | None = None) -> str:
        stop = stop or Event()
        now = datetime.now(UTC)
        with self._lease_lock:
            if self.cooldown_until is not None and self.cooldown_until > now:
                return "cooldown"
            self.cooldown_until = None
        try:
            value = self._request("/claim", {"idempotency_key": "claim-" + str(uuid4())})
            if value is None:
                return "idle"
            lease = WorkerLease.model_validate(value)
            if not 0 < (lease.lease_until - now).total_seconds() <= _MAX_LEASE_SECONDS + 60:
                raise ValueError
        except (ValueError, ValidationError):
            self._cooldown(datetime.fromtimestamp(now.timestamp() + _MAX_LEASE_SECONDS + 60, UTC))
            raise WorkerError("claim_response_invalid", uncertain=True) from None
        except WorkerError as error:
            if error.uncertain:
                self._cooldown(
                    datetime.fromtimestamp(now.timestamp() + _MAX_LEASE_SECONDS + 60, UTC)
                )
            raise

        heartbeat_stop = Event()
        heartbeat_failure: list[WorkerError] = []
        known_until = [lease.lease_until]

        def heartbeat() -> None:
            while not heartbeat_stop.wait(self._heartbeat_seconds):
                try:
                    value = self._request("/heartbeat", self._command(lease))
                    if not isinstance(value, dict) or not isinstance(value.get("lease_until"), str):
                        raise WorkerError("heartbeat_response_invalid", uncertain=True)
                    expiry = datetime.fromisoformat(value["lease_until"])
                    if (
                        expiry.tzinfo is None
                        or not 0
                        < (expiry - datetime.now(UTC)).total_seconds()
                        <= _MAX_LEASE_SECONDS + 60
                    ):
                        raise WorkerError("heartbeat_response_invalid", uncertain=True)
                    known_until[0] = expiry
                except (ValueError, WorkerError) as error:
                    heartbeat_failure.append(
                        error
                        if isinstance(error, WorkerError)
                        else WorkerError("heartbeat_response_invalid", uncertain=True)
                    )
                    heartbeat_stop.set()
                    return

        thread = Thread(target=heartbeat, name="native-workflow-heartbeat", daemon=True)
        thread.start()
        try:
            # Server max_steps is 32, so a corrupt response cannot cause an unbounded loop.
            for _ in range(33):
                if stop.is_set():
                    self._cooldown(known_until[0])
                    return "stopped"
                if heartbeat_failure:
                    raise heartbeat_failure[0]
                value = self._request("/step", self._command(lease))
                if (
                    not isinstance(value, dict)
                    or value.get("id") != str(lease.run_id)
                    or value.get("status")
                    not in {
                        "queued",
                        "running",
                        "waiting",
                        "completed",
                        "failed",
                        "unknown",
                        "cancelled",
                        "stale",
                    }
                ):
                    raise WorkerError("step_response_invalid", uncertain=True)
                if value["status"] != "running":
                    return str(value["status"])
            raise WorkerError("step_limit_exceeded")
        except WorkerError:
            self._cooldown(known_until[0])
            raise
        finally:
            heartbeat_stop.set()
            # The heartbeat uses bounded HTTP timeouts; do not close its client mid-flight.
            thread.join()
            if self.cooldown_until is not None:
                self._cooldown(known_until[0])


@contextmanager
def _shutdown_signals(stop: Event) -> Iterator[None]:
    def request_stop(_number: int, _frame: FrameType | None) -> None:
        stop.set()

    signals = [signal.SIGINT, signal.SIGTERM]
    previous = {number: signal.signal(number, request_stop) for number in signals}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--once", action="store_true", help="Process one claim, then exit.")
    parser.add_argument("--poll-seconds", type=float, default=5)
    args = parser.parse_args(argv)
    try:
        args.server_url = server_url(args.server_url)
    except ValueError as error:
        parser.error(str(error))
    if not isfinite(args.poll_seconds) or not 1 <= args.poll_seconds <= 60:
        parser.error("--poll-seconds must be between 1 and 60.")
    token = os.environ.get(TOKEN_ENV, "")
    try:
        worker = NativeWorker(args.server_url, token)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    stop = Event()
    try:
        with _shutdown_signals(stop):
            while not stop.is_set():
                try:
                    status = worker.cycle(stop)
                    if status not in {"idle", "cooldown"}:
                        print("Native workflow: " + status)
                except WorkerError as error:
                    print("Native workflow: " + error.code, file=sys.stderr)
                    if args.once or error.fatal:
                        return 1
                if args.once:
                    return 0
                stop.wait(args.poll_seconds)
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
