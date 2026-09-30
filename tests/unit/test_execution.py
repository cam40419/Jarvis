import io
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from simon.adapters.execution_backends import (
    DockerBackend,
    MachineBackend,
    _run_command,
    container_labels,
    docker_create_argv,
)
from simon.domain.execution import (
    EnvironmentDefinition,
    EnvironmentRequest,
    ExecutionCommand,
    ExecutionError,
    ExecutionResult,
)
from simon.services.execution import EnvironmentManager


class FakeBackend:
    def __init__(self):
        self.created = []
        self.released = []
        self.commands = []
        self.fail = False

    def allocate(self, lease):
        self.created.append(lease)
        if self.fail:
            raise ExecutionError("Lost allocation response")
        return "resource-" + str(lease.id)

    def heartbeat(self, lease):
        pass

    def release(self, lease):
        self.released.append(lease)

    def execute(self, lease, command):
        self.commands.append((lease, command))
        if self.fail:
            raise ExecutionError("Lost execution response")
        return ExecutionResult(exit_code=0, stdout="done")


def machine(**changes):
    return EnvironmentDefinition.model_validate(
        {
            "id": "design-machine",
            "kind": "machine",
            "os": "windows",
            "runner_url": "https://runner.example",
            "credential_env": "SIMON_TEST_RUNNER_TOKEN",
            "enabled": True,
            "capabilities": ["cad", "computer"],
            **changes,
        }
    )


def docker(**changes):
    return EnvironmentDefinition.model_validate(
        {
            "id": "build-container",
            "kind": "docker",
            "container_image": "example/worker:1",
            "enabled": True,
            "capabilities": ["code", "files"],
            **changes,
        }
    )


def request(**changes):
    return EnvironmentRequest.model_validate(
        {
            "workspace_id": str(uuid4()),
            "agent_id": "maker",
            "task_id": str(uuid4()),
            "attempt_id": str(uuid4()),
            **changes,
        }
    )


def manager(tmp_path, definition=None, backend=None):
    return EnvironmentManager(
        [definition or machine()],
        state_path=tmp_path / "leases.sqlite3",
        workspace_root=tmp_path / "workspaces",
        backends={"machine": backend or FakeBackend(), "docker": backend or FakeBackend()},
    )


def test_plan_is_pure_and_requires_compatible_enabled_environment(tmp_path):
    service = manager(tmp_path)
    plan = service.plan(request(os="windows", capabilities=["cad"]))
    assert plan.kind == "machine"
    assert not (tmp_path / "leases.sqlite3").exists()
    assert not (tmp_path / "workspaces").exists()
    for wanted in (request(os="linux"), request(capabilities=["render"])):
        with pytest.raises(ExecutionError, match="satisfies"):
            service.plan(wanted)
    with pytest.raises(ExecutionError, match="satisfies"):
        manager(tmp_path, machine(enabled=False)).plan(request())


@pytest.mark.parametrize(
    "url",
    [
        "http://runner.example",
        "https://user:secret@runner.example",
        "https://runner.example?token=secret",
        "file:///machine",
    ],
)
def test_machine_config_rejects_insecure_or_credential_bearing_urls(url):
    with pytest.raises(ValidationError):
        machine(runner_url=url)


def test_resource_policy_and_agent_path_are_validated():
    with pytest.raises(ValidationError):
        machine(max_concurrency=2)
    with pytest.raises(ValidationError):
        docker(container_image="--privileged")
    with pytest.raises(ValidationError):
        docker(gpu_devices=["all"])
    with pytest.raises(ValidationError):
        request(agent_id="../../another-user")
    with pytest.raises(ValidationError):
        ExecutionCommand(argv=("",))
    with pytest.raises(ValidationError):
        ExecutionCommand(argv=("echo", "a\x00b"))


def test_machine_lease_is_exclusive_durable_and_fenced(tmp_path):
    backend = FakeBackend()
    service = manager(tmp_path, backend=backend)
    original = request()
    lease = service.allocate(original)
    restarted = manager(tmp_path, backend=backend)
    assert restarted.allocate(original) == lease
    assert len(backend.created) == 1
    with pytest.raises(ExecutionError, match="capacity"):
        restarted.allocate(request())
    with pytest.raises(ExecutionError, match="does not own"):
        restarted.release(lease.id, attempt_id=uuid4(), fencing_token=lease.fencing_token)
    with pytest.raises(ExecutionError, match="does not own"):
        restarted.release(lease.id, attempt_id=original.attempt_id, fencing_token=99)
    released = restarted.release(
        lease.id, attempt_id=original.attempt_id, fencing_token=lease.fencing_token
    )
    assert released.status == "released"
    assert (
        service.release(lease.id, attempt_id=original.attempt_id, fencing_token=lease.fencing_token)
        == released
    )
    assert len(backend.released) == 1
    next_lease = service.allocate(request())
    assert next_lease.fencing_token > lease.fencing_token


def test_atomic_reservation_across_managers_precedes_external_allocation(tmp_path):
    started, finish = Event(), Event()

    class SlowBackend(FakeBackend):
        def allocate(self, lease):
            started.set()
            assert finish.wait(timeout=5)
            return super().allocate(lease)

    backend = SlowBackend()
    first = manager(tmp_path, backend=backend)
    second = manager(tmp_path, backend=backend)
    original = request()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(first.allocate, original)
        try:
            assert started.wait(timeout=5)
            assert second.allocate(original).status == "allocating"
            with pytest.raises(ExecutionError, match="capacity"):
                second.allocate(request())
        finally:
            finish.set()
        assert future.result().status == "active"
    assert len(backend.created) == 1


def test_lost_allocate_response_is_not_retried_or_reassigned(tmp_path):
    backend = FakeBackend()
    backend.fail = True
    service = manager(tmp_path, backend=backend)
    original = request()
    with pytest.raises(ExecutionError, match="Lost allocation"):
        service.allocate(original)
    uncertain = manager(tmp_path, backend=backend).allocate(original)
    assert uncertain.status == "unknown"
    assert len(backend.created) == 1
    with pytest.raises(ExecutionError, match="capacity"):
        service.allocate(request())
    service.release(
        uncertain.id, attempt_id=original.attempt_id, fencing_token=uncertain.fencing_token
    )
    assert service.get(uncertain.id).status == "released"


def test_same_attempt_cannot_be_rebound_to_another_owner(tmp_path):
    service = manager(tmp_path)
    original = request()
    service.allocate(original)
    with pytest.raises(ExecutionError, match="different execution plan"):
        service.allocate(request(attempt_id=str(original.attempt_id)))


def test_docker_plans_have_separate_workspaces_and_constrained_argv(tmp_path):
    backend = FakeBackend()
    service = manager(tmp_path, docker(max_concurrency=2), backend)
    one = service.allocate(request())
    two = service.allocate(request())
    assert one.plan.workspace_path != two.plan.workspace_path
    assert one.plan.workspace_path.is_dir()
    argv = docker_create_argv(one)
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert argv[argv.index("--memory") + 1] == "2048m"
    assert "--read-only" in argv
    assert "--privileged" not in argv
    assert "--gpus" not in argv
    assert "docker.sock" not in " ".join(argv)
    assert argv.count("--mount") == 1
    service.release(one.id, attempt_id=one.plan.request.attempt_id, fencing_token=one.fencing_token)
    assert one.plan.workspace_path.is_dir(), "Release must preserve generated artifacts"


def test_docker_gpu_access_requires_explicit_device_indexes(tmp_path):
    lease = manager(tmp_path, docker(gpu_devices=["0", "2"])).allocate(request())
    argv = docker_create_argv(lease)
    assert argv[argv.index("--gpus") + 1] == '"device=0,2"'


def container_record(lease, **changes):
    return {
        "Id": "a" * 64,
        "Config": {"Labels": container_labels(lease)},
        "State": {"Running": True},
        **changes,
    }


def test_docker_verifies_owned_labels_before_cleanup(tmp_path):
    lease = manager(tmp_path, docker()).allocate(request())
    lease = lease.model_copy(update={"resource_handle": "a" * 64})
    commands = []

    def run(argv):
        commands.append(argv)
        if argv[1] == "container":
            return subprocess.CompletedProcess(argv, 0, "a" * 64, "")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps([container_record(lease, Config={"Labels": {}})]), ""
        )

    with pytest.raises(ExecutionError, match="ownership"):
        DockerBackend(run=run).release(lease)
    assert len(commands) == 2
    assert commands[0][1] == "inspect"
    assert all(argv[1] != "rm" for argv in commands)


def test_docker_release_can_reconcile_a_lost_removal_response(tmp_path):
    lease = manager(tmp_path, docker()).allocate(request())
    lease = lease.model_copy(update={"resource_handle": "a" * 64})
    commands = []

    def run(argv):
        commands.append(argv)
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(argv, 1, "", "No such container")
        return subprocess.CompletedProcess(argv, 0, "", "")

    DockerBackend(run=run).release(lease)
    assert len(commands) == 2
    assert commands[1][1:3] == ["container", "ls"]
    assert "id=" + "a" * 64 in commands[1]


def test_interrupted_operation_requires_explicit_administrative_recovery(tmp_path):
    service = manager(tmp_path)
    lease = service.allocate(request())
    pending = lease.model_copy(update={"status": "executing"})
    service._save(pending, expected_status="active")
    with pytest.raises(ExecutionError, match="in progress"):
        service.release(
            lease.id,
            attempt_id=lease.plan.request.attempt_id,
            fencing_token=lease.fencing_token,
        )
    released = service.release(
        lease.id,
        attempt_id=lease.plan.request.attempt_id,
        fencing_token=lease.fencing_token,
        recover_interrupted=True,
    )
    assert released.status == "released"


def test_docker_exec_passes_literal_arguments_after_verifying_ownership(tmp_path):
    lease = manager(tmp_path, docker()).allocate(request())
    lease = lease.model_copy(update={"resource_handle": "a" * 64})
    commands = []

    def run(argv):
        return subprocess.CompletedProcess(argv, 0, json.dumps([container_record(lease)]), "")

    def execute(argv, command):
        commands.append((argv, command))
        return ExecutionResult(exit_code=0, stdout="literal")

    backend = DockerBackend(run=run, command_runner=execute)
    command = ExecutionCommand(argv=("echo", "$(Get-Content secret)", "; rm -rf /"))
    result = backend.execute(lease, command)
    assert result.stdout == "literal"
    assert commands[0][0][-3:] == list(command.argv)
    assert commands[0][0][:4] == ["docker", "exec", "--workdir", "/workspace"]


def test_command_runner_bounds_both_output_streams_without_using_a_shell(monkeypatch):
    invocations = []

    class Process:
        stdout = io.BytesIO(b"\xff" * 8000)
        stderr = io.BytesIO(b"error" * 8000)
        returncode = 0

        def wait(self, timeout):
            return 0

    def popen(argv, **kwargs):
        invocations.append((argv, kwargs))
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    command = ExecutionCommand(argv=("echo", "$HOME"), max_output_bytes=1024)
    result = _run_command(["docker", "exec", "owned", *command.argv], command)
    assert result.truncated
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= 1024
    assert invocations[0][1]["shell"] is False
    assert invocations[0][0][-1] == "$HOME"


def test_command_runner_timeout_kills_cli_and_reports_remote_uncertainty(monkeypatch):
    class Process:
        stdout = io.BytesIO(b"")
        stderr = io.BytesIO(b"")
        returncode = 0
        killed = False

        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("docker exec", timeout)
            return 0

        def kill(self):
            self.killed = True

    process = Process()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(ExecutionError, match="remote outcome is unknown"):
        _run_command(["docker", "exec", "owned", "tool"], ExecutionCommand(argv=("tool",)))
    assert process.killed


def test_execute_rejects_other_attempt_and_quarantines_unknown_outcome(tmp_path):
    backend = FakeBackend()
    service = manager(tmp_path, backend=backend)
    original = request()
    lease = service.allocate(original)
    command = ExecutionCommand(argv=("tool", "--input", "file"))
    with pytest.raises(ExecutionError, match="does not own"):
        service.execute(lease.id, command, attempt_id=uuid4(), fencing_token=lease.fencing_token)
    assert backend.commands == []
    result = service.execute(
        lease.id, command, attempt_id=original.attempt_id, fencing_token=lease.fencing_token
    )
    assert result.exit_code == 0
    backend.fail = True
    with pytest.raises(ExecutionError, match="Lost execution"):
        service.execute(
            lease.id, command, attempt_id=original.attempt_id, fencing_token=lease.fencing_token
        )
    assert service.get(lease.id).status == "unknown"
    with pytest.raises(ExecutionError, match="active"):
        service.execute(
            lease.id, command, attempt_id=original.attempt_id, fencing_token=lease.fencing_token
        )
    assert len(backend.commands) == 2


def test_machine_protocol_authenticates_identity_and_bounded_command(tmp_path, monkeypatch):
    monkeypatch.setenv("SIMON_TEST_RUNNER_TOKEN", "synthetic-key")
    lease = manager(tmp_path).allocate(request())
    calls = []

    def handler(incoming):
        calls.append(incoming)
        body = json.loads(incoming.content)
        assert incoming.headers["authorization"] == "Bearer synthetic-key"
        assert body["lease_id"] == str(lease.id)
        assert body["fencing_token"] == lease.fencing_token
        assert "synthetic-key" not in incoming.content.decode()
        return httpx.Response(
            200,
            json={
                "lease_id": str(lease.id),
                "fencing_token": lease.fencing_token,
                "resource_handle": "desktop-lease",
                "result": {"exit_code": 0, "stdout": "done"},
            },
        )

    backend = MachineBackend(transport=httpx.MockTransport(handler))
    assert backend.allocate(lease) == "desktop-lease"
    result = backend.execute(lease, ExecutionCommand(argv=("design.exe", "input.cad")))
    assert result.stdout == "done"
    backend.heartbeat(lease)
    backend.release(lease)
    assert len(calls) == 4
    assert calls[1].url.path.endswith("/execute")
    assert json.loads(calls[1].content)["command"]["timeout_seconds"] == 60


@pytest.mark.parametrize("failure", ["redirect", "timeout", "wrong_owner", "overflow"])
def test_machine_failure_is_not_retried(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("SIMON_TEST_RUNNER_TOKEN", "synthetic-key")
    lease = manager(tmp_path).allocate(request())
    calls = []

    def handler(incoming):
        calls.append(incoming)
        if failure == "timeout":
            raise httpx.ReadTimeout("unknown")
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://other.example"})
        if failure == "overflow":
            return httpx.Response(200, content=b"x" * 70000)
        return httpx.Response(200, json={"lease_id": str(uuid4()), "fencing_token": 1})

    with pytest.raises(ExecutionError):
        MachineBackend(transport=httpx.MockTransport(handler)).allocate(lease)
    assert len(calls) == 1
