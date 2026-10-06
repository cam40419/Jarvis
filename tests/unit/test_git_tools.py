from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from simon.adapters._git_runner import (
    branch_name,
    check_repository,
    checked_path,
    git_command,
    validate_arguments,
)
from simon.adapters.git_tools import GIT_CAPABILITIES, GitToolTransport, git_tool_definitions
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.errors import AuthorizationError
from simon.domain.execution import (
    EnvironmentDefinition,
    EnvironmentLease,
    EnvironmentPlan,
    EnvironmentRequest,
    ExecutionCommand,
    ExecutionError,
    ExecutionResult,
)
from simon.domain.models import utc_now
from simon.domain.tool_catalog import (
    ToolCatalogError,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutionError,
)


class RecordingManager:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, ExecutionCommand, UUID, int]] = []
        self.fail = False
        self.result = ExecutionResult(exit_code=0, stdout="## main\n")

    def execute(
        self,
        lease_id: UUID,
        command: ExecutionCommand,
        *,
        attempt_id: UUID,
        fencing_token: int,
    ) -> ExecutionResult:
        self.calls.append((lease_id, command, attempt_id, fencing_token))
        if self.fail:
            raise ExecutionError("sensitive backend detail")
        return self.result


def setup(
    operation: str = "status",
) -> tuple[
    RecordingManager,
    EnvironmentLease,
    ToolDefinition,
    ToolExecutionContext,
    GitToolTransport,
]:
    definition = EnvironmentDefinition(
        id="git-worker",
        kind="docker",
        container_image="simon-git-worker:local",
        enabled=True,
        capabilities=GIT_CAPABILITIES,
    )
    request = EnvironmentRequest(
        workspace_id=uuid4(),
        agent_id="coder",
        task_id=uuid4(),
        attempt_id=uuid4(),
        capabilities=GIT_CAPABILITIES,
    )
    lease = EnvironmentLease(
        id=uuid4(),
        plan=EnvironmentPlan(
            environment_id=definition.id,
            kind="docker",
            request=request,
            workspace_path=Path("unused"),
            network="none",
            cpu_limit=2,
            memory_mb=2048,
            gpu_devices=(),
        ),
        definition=definition,
        fencing_token=3,
        status="active",
        resource_handle="owned",
        created_at=utc_now(),
        heartbeat_at=utc_now(),
    )
    tool = next(
        item for item in git_tool_definitions(enabled=True) if item.id == "git." + operation
    )
    context = ToolExecutionContext(
        actor_id=uuid4(),
        workspace_id=request.workspace_id,
        run_id=uuid4(),
        agent_id="coder",
        allowed_tool_ids=frozenset({tool.id}),
        scopes=tool.required_scopes,
        environment_capabilities=GIT_CAPABILITIES,
        authorized_action=tool.action_policy,
    )
    manager = RecordingManager()
    transport = GitToolTransport(manager, lease, actor_id=context.actor_id, run_id=context.run_id)
    return manager, lease, tool, context, transport


def test_definitions_default_to_unconfigured_and_cover_real_operations() -> None:
    definitions = git_tool_definitions()
    assert {item.id for item in definitions} == {
        "git.status",
        "git.diff",
        "git.log",
        "git.branches",
        "git.init",
        "git.branch",
        "git.switch",
        "git.add",
        "git.commit",
    }
    assert all(not item.enabled and not item.configured for item in definitions)


def test_registry_dispatches_guard_inside_exact_owned_lease() -> None:
    manager, lease, tool, context, transport = setup()
    registry = TransportRegistry()
    registry.register("git", transport)
    result = registry.execute(tool, {"repository": "source"}, context)
    assert result.output["stdout"] == "## main\n"
    lease_id, command, attempt, fence = manager.calls[0]
    assert (lease_id, attempt, fence) == (lease.id, lease.plan.request.attempt_id, 3)
    assert command.argv[:3] == ("/usr/local/bin/python3", "-I", "-c")
    assert json.loads(command.argv[4])["arguments"] == {"repository": "source"}
    assert 'Path("/workspace")' in command.argv[3]
    assert command.timeout_seconds == 60
    assert command.max_output_bytes == 65536


@pytest.mark.parametrize("field", ["actor_id", "workspace_id", "run_id", "agent_id"])
def test_cannot_cross_worker_assignment(field: str) -> None:
    manager, _, tool, context, transport = setup()
    value: Any = "other" if field == "agent_id" else uuid4()
    with pytest.raises(AuthorizationError, match="worker lease"):
        transport(tool, {}, context.model_copy(update={field: value}))
    assert manager.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"scopes": frozenset()},
        {"allowed_tool_ids": frozenset()},
        {"authorized_action": "read"},
    ],
)
def test_writes_require_assignment_scope_and_action_authorization(change: dict[str, Any]) -> None:
    manager, _, tool, context, transport = setup("commit")
    with pytest.raises(AuthorizationError):
        transport(tool, {"message": "Example"}, context.model_copy(update=change))
    assert manager.calls == []


def test_relabelled_write_cannot_evade_scope_check() -> None:
    manager, _, tool, context, transport = setup("commit")
    tool = tool.model_copy(
        update={"required_scopes": frozenset(), "action_policy": "read", "side_effect": False}
    )
    with pytest.raises(AuthorizationError):
        transport(tool, {"message": "Example"}, context.model_copy(update={"scopes": frozenset()}))
    assert manager.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/tmp",
        "C:/tmp",
        "a\\b",
        "a//b",
        ".git",
        "a/.git/config",
        "--help",
        "bad\nname",
        "a/../b",
    ],
)
def test_paths_rejected_before_runner_dispatch(path: str) -> None:
    manager, _, tool, context, transport = setup()
    with pytest.raises(ToolCatalogError):
        transport(tool, {"repository": path}, context)
    assert manager.calls == []


@pytest.mark.parametrize("name", ["--help", "a..b", "a@{x}", "a.lock", ".foo", "a//b", "HEAD"])
def test_branch_names_cannot_be_options_or_special_refs(name: str) -> None:
    with pytest.raises(ValueError):
        branch_name(name)


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("status", {"argv": ["git", "push"]}),
        ("push", {}),
        ("log", {"limit": 101}),
        ("log", {"limit": True}),
        ("diff", {"staged": 1}),
        ("add", {"paths": []}),
        ("add", {"paths": ["../escape"]}),
        ("commit", {"message": "\x00"}),
    ],
)
def test_invalid_arguments_are_not_interpreted_as_command_options(
    operation: str,
    arguments: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        validate_arguments(operation, arguments)


def test_failure_redacted_without_retry_and_write_is_uncertain() -> None:
    manager, _, tool, context, transport = setup("commit")
    manager.fail = True
    with pytest.raises(ToolExecutionError, match="Leased Git command failed") as error:
        transport(tool, {"message": "Example"}, context)
    assert error.value.unknown
    assert "sensitive" not in str(error.value)
    assert len(manager.calls) == 1


def test_guard_timeout_marks_commit_outcome_uncertain() -> None:
    manager, _, tool, context, transport = setup("commit")
    manager.result = ExecutionResult(exit_code=124)
    with pytest.raises(ToolExecutionError, match="timed out") as error:
        transport(tool, {"message": "Example"}, context)
    assert error.value.unknown


def test_actual_lease_capabilities_are_checked() -> None:
    manager, lease, tool, context, _ = setup()
    lease = lease.model_copy(
        update={
            "definition": lease.definition.model_copy(
                update={"capabilities": frozenset({"python"})},
            )
        }
    )
    transport = GitToolTransport(manager, lease, actor_id=context.actor_id, run_id=context.run_id)
    with pytest.raises(ToolCatalogError, match="Git and Python"):
        transport(tool, {}, context)
    assert manager.calls == []


def test_command_contract_disables_executable_configuration(tmp_path: Path) -> None:
    arguments = validate_arguments("commit", {"message": "Literal $(secret) `text`; example"})
    argv = git_command("commit", arguments, tmp_path, author_name="Agent", author_email="a@b")
    assert "core.hooksPath=" + os.devnull in argv
    assert "core.fsmonitor=false" in argv
    assert "protocol.allow=never" in argv
    assert "commit.gpgSign=false" in argv
    assert argv[-2:] == [arguments["message"], "--"]


@pytest.mark.parametrize(
    "config",
    [
        "[core]\n bare = false\n hooksPath = /tmp/hooks\n",
        "[core]\n bare = false\n fsmonitor = command\n",
        "[include]\n path = /tmp/config\n",
        '[filter "evil"]\n clean = command\n',
        "[core]\n worktree = /tmp/escape\n",
        "[core]\n bare = true\n",
    ],
)
def test_unsafe_repository_configuration_rejected(tmp_path: Path, config: str) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(config)
    with pytest.raises(ValueError):
        check_repository(tmp_path)


def test_linked_worktree_is_rejected(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /outside")
    with pytest.raises(ValueError, match="standalone"):
        check_repository(tmp_path)


def test_symlink_path_cannot_escape_workspace(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("This platform requires symlink privileges")
    with pytest.raises(ValueError, match="Symbolic links"):
        checked_path(tmp_path, "linked")


def test_real_git_lifecycle_and_hooks_are_disabled(tmp_path: Path) -> None:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git executable is unavailable")
    script = (
        "import json,sys; from pathlib import Path; "
        "from simon.adapters._git_runner import run; "
        "sys.exit(run(json.loads(sys.argv[1]), Path(sys.argv[2]), executable=sys.argv[3]))"
    )

    def invoke(operation: str, **arguments: Any) -> subprocess.CompletedProcess[str]:
        request = {
            "operation": operation,
            "arguments": arguments,
            "author_name": "Test Agent",
            "author_email": "test@example.invalid",
            "timeout_seconds": 20,
        }
        return subprocess.run(
            [sys.executable, "-c", script, json.dumps(request), str(tmp_path), executable],
            text=True,
            capture_output=True,
            timeout=25,
            check=False,
        )

    result = invoke("init")
    assert result.returncode == 0, result.stderr
    (tmp_path / "source.txt").write_text("first\n", encoding="utf-8")
    assert invoke("status").returncode == 0
    assert "source.txt" in invoke("status").stdout
    assert invoke("add", paths=["source.txt"]).returncode == 0
    assert "+first" in invoke("diff", staged=True).stdout
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 42\n")
    hook.chmod(0o755)
    result = invoke("commit", message="First local revision")
    assert result.returncode == 0, result.stderr
    assert "First local revision" in invoke("log", limit=1).stdout
    assert invoke("branch", name="feature/example").returncode == 0
    assert "feature/example" in invoke("branches").stdout
    result = invoke("switch", name="feature/example")
    assert result.returncode == 0, result.stderr
    assert "feature/example" in invoke("status").stdout
    assert invoke("init").returncode != 0
    (tmp_path / "source.txt").write_text("second\n", encoding="utf-8")
    assert "+second" in invoke("diff").stdout
