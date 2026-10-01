"""Verify the Git worker image using a real owned lease and synthetic local files.

Run after building deploy/Dockerfile.git-worker. No remote repository is contacted.
The smoke workspace/journal are retained under .local/agents for inspection.
"""

from pathlib import Path
from uuid import uuid4

from simon.adapters.git_tools import GIT_CAPABILITIES, GitToolTransport, git_tool_definitions
from simon.adapters.tool_transports import TransportRegistry
from simon.domain.execution import EnvironmentDefinition, EnvironmentRequest, ExecutionCommand
from simon.domain.tool_catalog import ToolExecutionContext
from simon.services.execution import EnvironmentManager


def main() -> None:
    state = (Path(".local/agents") / ("git-smoke-" + uuid4().hex)).resolve()
    manager = EnvironmentManager(
        [EnvironmentDefinition(
            id="git-smoke", kind="docker", enabled=True,
            container_image="simon-git-worker:local", capabilities=GIT_CAPABILITIES,
            network="none",
        )], state_path=state / "leases.sqlite3", workspace_root=state / "workspaces",
    )
    request = EnvironmentRequest(
        workspace_id=uuid4(), agent_id="git-smoke", task_id=uuid4(), attempt_id=uuid4(),
        capabilities=GIT_CAPABILITIES, os="linux",
    )
    lease = manager.allocate(request, environment_id="git-smoke")
    ownership = {"attempt_id": request.attempt_id, "fencing_token": lease.fencing_token}
    context = ToolExecutionContext(
        actor_id=uuid4(), household_id=request.workspace_id, run_id=uuid4(), agent_id="git-smoke",
        scopes=frozenset({"jobs:read", "jobs:write"}),
        allowed_tool_ids=frozenset(item.id for item in git_tool_definitions()),
        environment_capabilities=GIT_CAPABILITIES, authorized_action="write",
    )
    registry = TransportRegistry()
    registry.register("git", GitToolTransport(
        manager, lease, actor_id=context.actor_id, run_id=context.run_id,
    ))
    definitions = {item.id: item for item in git_tool_definitions(enabled=True)}
    try:
        created = manager.execute(lease.id, ExecutionCommand(argv=(
            "/usr/local/bin/python3", "-I", "-c",
            "from pathlib import Path; Path('/workspace/example.txt').write_text('sample\\n')",
        )), **ownership)
        if created.exit_code:
            raise RuntimeError("Could not create the synthetic worker input")
        operations = (
            ("init", {}), ("status", {}), ("add", {"paths": ["example.txt"]}),
            ("diff", {"staged": True}), ("commit", {"message": "Synthetic smoke revision"}),
            ("branch", {"name": "example"}), ("switch", {"name": "example"}),
            ("branches", {}), ("log", {"limit": 1}),
        )
        for operation, arguments in operations:
            result = registry.execute(definitions["git." + operation], arguments, context)
            if result.output["exit_code"]:
                raise RuntimeError(f"git.{operation}: {result.output['stderr']}")
            print(f"git.{operation}: passed")
    finally:
        manager.release(lease.id, **ownership)
    print(f"Lease released. Synthetic workspace retained at {state}")


if __name__ == "__main__":
    main()
