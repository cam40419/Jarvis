"""Container allocation and a narrow, authenticated dedicated-machine protocol.

Machine endpoints: POST /v1/leases, /v1/leases/{id}/heartbeat,
/v1/leases/{id}/execute and /v1/leases/{id}/release. Every request carries the lease ID,
fencing token,
workspace/agent/task/attempt identity and immutable resource policy. A runner MUST
enforce one exclusive lease, remember the greatest fencing token, make repeated
allocation/release idempotent, and refuse stale tokens. Allocation returns
{"lease_id": "...", "fencing_token": 1, "resource_handle": "..."}; heartbeat and
release return the same identity. Execute also receives a command with argv,
timeout_seconds and max_output_bytes; its reply includes result with exit_code,
stdout, stderr and truncated. The runner enforces timeout/output limits, rejects
commands after release, and preserves lease tombstones. It enforces resource limits, workspace
isolation and egress policy locally. This adapter does not provision machines or
install that runner. An external runner must implement this contract before use.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from threading import Lock, Thread
from typing import IO, Any, Protocol

import httpx

from simon.domain.execution import (
    EnvironmentLease,
    ExecutionCommand,
    ExecutionError,
    ExecutionResult,
)


class ExecutionBackend(Protocol):
    def allocate(self, lease: EnvironmentLease) -> str: ...

    def heartbeat(self, lease: EnvironmentLease) -> None: ...

    def release(self, lease: EnvironmentLease) -> None: ...

    def execute(self, lease: EnvironmentLease, command: ExecutionCommand) -> ExecutionResult: ...


def container_name(lease: EnvironmentLease) -> str:
    return "simon-" + lease.id.hex


def container_labels(lease: EnvironmentLease) -> dict[str, str]:
    return {
        "simon.managed": "true",
        "simon.lease": str(lease.id),
        "simon.attempt": str(lease.plan.request.attempt_id),
        "simon.fence": str(lease.fencing_token),
        "simon.workspace": str(lease.plan.request.workspace_id),
    }


def docker_create_argv(lease: EnvironmentLease) -> list[str]:
    definition = lease.definition
    if definition.kind != "docker" or not definition.container_image:
        raise ExecutionError("The lease does not target a container")
    source = str(lease.plan.workspace_path)
    if "," in source or "\n" in source or "\r" in source:
        raise ExecutionError("The configured workspace path is not a valid Docker mount source")
    argv = [
        "docker",
        "create",
        "--name",
        container_name(lease),
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--cpus",
        str(definition.cpu_limit),
        "--memory",
        f"{definition.memory_mb}m",
        "--memory-swap",
        f"{definition.memory_mb}m",
        "--network",
        definition.network,
        "--user",
        f"{definition.container_uid}:{definition.container_gid}",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--mount",
        f"type=bind,source={source},target=/workspace",
        "--workdir",
        "/workspace",
        "--entrypoint",
        "/bin/sleep",
    ]
    for name, value in container_labels(lease).items():
        argv.extend(("--label", f"{name}={value}"))
    if definition.gpu_devices:
        # Docker's flag value is CSV; quoted device lists remain a single CSV field.
        argv.extend(("--gpus", '"device=' + ",".join(definition.gpu_devices) + '"'))
    argv.extend((definition.container_image, "infinity"))
    return argv


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)


def _run_command(argv: list[str], command: ExecutionCommand) -> ExecutionResult:
    """Drain both pipes while retaining only a bounded combined output buffer."""
    output = [bytearray(), bytearray()]
    output_lock = Lock()
    truncated = False

    def drain(stream: IO[bytes], index: int) -> None:
        nonlocal truncated
        try:
            while data := stream.read(8192):
                with output_lock:
                    remaining = max(0, command.max_output_bytes - sum(map(len, output)))
                    output[index].extend(data[:remaining])
                    truncated = truncated or len(data) > remaining
        finally:
            stream.close()

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError as exc:
        raise ExecutionError("Container execution could not start") from exc
    assert process.stdout is not None and process.stderr is not None
    readers = [
        Thread(target=drain, args=(process.stdout, 0), daemon=True),
        Thread(target=drain, args=(process.stderr, 1), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        process.wait(timeout=command.timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=5)
        raise ExecutionError("Container command timed out; its remote outcome is unknown") from exc
    finally:
        for reader in readers:
            reader.join(timeout=5)
    if any(reader.is_alive() for reader in readers):
        raise ExecutionError("Container output did not close; its remote outcome is unknown")
    decoded: list[str] = []
    remaining_bytes = command.max_output_bytes
    for buffer in output:
        encoded = buffer.decode("utf-8", errors="replace").encode("utf-8")
        if len(encoded) > remaining_bytes:
            truncated = True
        value = encoded[:remaining_bytes].decode("utf-8", errors="ignore")
        decoded.append(value)
        remaining_bytes -= len(value.encode("utf-8"))
    return ExecutionResult(
        exit_code=process.returncode,
        stdout=decoded[0],
        stderr=decoded[1],
        truncated=truncated,
    )


class DockerBackend:
    def __init__(
        self,
        run: Callable[[list[str]], subprocess.CompletedProcess[str]] = _run,
        command_runner: Callable[[list[str], ExecutionCommand], ExecutionResult] = _run_command,
    ) -> None:
        self.run = run
        self.command_runner = command_runner

    def _invoke(self, argv: list[str]) -> str:
        try:
            result = self.run(argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutionError("Container command failed; reconcile its lease") from exc
        if result.returncode:
            raise ExecutionError("Container command was rejected; reconcile its lease")
        return result.stdout.strip()

    def allocate(self, lease: EnvironmentLease) -> str:
        handle = self._invoke(docker_create_argv(lease))
        if len(handle) != 64 or any(c not in "0123456789abcdef" for c in handle):
            raise ExecutionError("Docker returned an invalid container identifier")
        self._invoke(["docker", "start", handle])
        return handle

    def _owned_container(self, lease: EnvironmentLease) -> dict[str, Any]:
        # The deterministic name permits cleanup after a lost create/start reply.
        output = self._invoke(["docker", "inspect", lease.resource_handle or container_name(lease)])
        try:
            records = json.loads(output)
            if not isinstance(records, list) or len(records) != 1:
                raise ValueError
            record: dict[str, Any] = records[0]
            labels = record["Config"]["Labels"]
            handle = record["Id"]
            if (
                not isinstance(handle, str)
                or len(handle) != 64
                or any(c not in "0123456789abcdef" for c in handle)
                or any(labels.get(key) != value for key, value in container_labels(lease).items())
                or (lease.resource_handle is not None and handle != lease.resource_handle)
            ):
                raise ValueError
            return record
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ExecutionError("Container ownership could not be verified") from exc

    def heartbeat(self, lease: EnvironmentLease) -> None:
        record = self._owned_container(lease)
        if record.get("State", {}).get("Running") is not True:
            raise ExecutionError("The owned container is not running")

    def release(self, lease: EnvironmentLease) -> None:
        try:
            record = self._owned_container(lease)
        except ExecutionError:
            # A successful query proving absence also reconciles a lost remove reply.
            # A daemon failure is not evidence that the resource no longer exists.
            selector = (
                "id=" + lease.resource_handle
                if lease.resource_handle
                else "name=^/" + container_name(lease) + "$"
            )
            remaining = self._invoke(
                [
                    "docker",
                    "container",
                    "ls",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    selector,
                    "--format",
                    "{{.ID}}",
                ]
            )
            if remaining:
                raise
            return
        self._invoke(["docker", "rm", "--force", str(record["Id"])])

    def execute(self, lease: EnvironmentLease, command: ExecutionCommand) -> ExecutionResult:
        record = self._owned_container(lease)
        if record.get("State", {}).get("Running") is not True:
            raise ExecutionError("The owned container is not running")
        definition = lease.definition
        return self.command_runner(
            [
                "docker",
                "exec",
                "--workdir",
                "/workspace",
                "--user",
                f"{definition.container_uid}:{definition.container_gid}",
                str(record["Id"]),
                *command.argv,
            ],
            command,
        )


class MachineBackend:
    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.transport = transport
        self._environ = os.environ if environ is None else environ

    def _request(
        self, lease: EnvironmentLease, operation: str, command: ExecutionCommand | None = None
    ) -> dict[str, Any]:
        definition = lease.definition
        token = self._environ.get(definition.credential_env or "", "")
        if definition.kind != "machine" or not definition.runner_url or not token:
            raise ExecutionError("Machine runner credentials are unavailable")
        path = "/v1/leases" if operation == "allocate" else f"/v1/leases/{lease.id}/{operation}"
        payload = {
            "lease_id": str(lease.id),
            "fencing_token": lease.fencing_token,
            "request": lease.plan.request.model_dump(mode="json"),
            "policy": definition.model_dump(mode="json", exclude={"credential_env", "runner_url"}),
        }
        if command is not None:
            payload["command"] = command.model_dump(mode="json")
        try:
            with (
                httpx.Client(
                    timeout=(command.timeout_seconds + 15) if command else 30,
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport,
                ) as client,
                client.stream(
                    "POST",
                    definition.runner_url.rstrip("/") + path,
                    headers={"Authorization": "Bearer " + token},
                    json=payload,
                ) as response,
            ):
                if response.status_code < 200 or response.status_code >= 300:
                    raise ExecutionError("Machine runner rejected the operation")
                content = bytearray()
                for part in response.iter_bytes():
                    content.extend(part)
                    # JSON escaping can expand one output byte to six wire bytes.
                    if len(content) > (6 * command.max_output_bytes + 65536 if command else 65536):
                        raise ExecutionError("Machine runner response exceeded its limit")
                body: dict[str, Any] = json.loads(content)
                if (
                    not isinstance(body, dict)
                    or body.get("lease_id") != str(lease.id)
                    or body.get("fencing_token") != lease.fencing_token
                ):
                    raise ExecutionError("Machine runner returned another lease identity")
                return body
        except (httpx.HTTPError, ValueError) as exc:
            raise ExecutionError("Machine runner outcome is unknown; reconcile its lease") from exc

    def allocate(self, lease: EnvironmentLease) -> str:
        handle = self._request(lease, "allocate").get("resource_handle")
        if not isinstance(handle, str) or not handle or len(handle) > 256:
            raise ExecutionError("Machine runner omitted its resource identifier")
        return handle

    def heartbeat(self, lease: EnvironmentLease) -> None:
        self._request(lease, "heartbeat")

    def release(self, lease: EnvironmentLease) -> None:
        self._request(lease, "release")

    def execute(self, lease: EnvironmentLease, command: ExecutionCommand) -> ExecutionResult:
        result = ExecutionResult.model_validate(
            self._request(lease, "execute", command).get("result")
        )
        if len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) > (
            command.max_output_bytes
        ):
            raise ExecutionError("Machine runner ignored the command output limit")
        return result
