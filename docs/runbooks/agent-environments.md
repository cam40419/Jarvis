# Agent execution environments

Simon can plan an environment, reserve it for one execution attempt, run a bounded
command, and release it. Docker containers have separate workspaces for each
agent/task/attempt. A registered machine has one exclusive lease at a time.
These are trusted worker APIs; callers must authorize the assignment and its
tools before invoking them. They are not an unrestricted public command endpoint.
The [team dispatcher](agent-execution.md) now binds these interfaces to each task,
including shared capacity checks, lease tracking, heartbeat and owned cleanup.

## Plan without starting resources

Run this example from the repository root in Simon's Python environment.
Construction and `plan()` do not create directories, open the lease journal,
contact a machine, or invoke Docker. Capability names are configured labels; a
plan checks compatibility, not whether application binaries are installed.

```python
from pathlib import Path
from uuid import uuid4

from simon.domain.execution import (
    EnvironmentDefinition,
    EnvironmentRequest,
    ExecutionCommand,
)
from simon.services.execution import EnvironmentManager

state_root = Path(".local/agents").resolve()
manager = EnvironmentManager(
    [EnvironmentDefinition(
        id="python-worker",
        kind="docker",
        enabled=True,
        container_image="python:3.11-slim",
        capabilities=frozenset({"files", "python"}),
        cpu_limit=2,
        memory_mb=2048,
        network="none",
        max_concurrency=4,
    )],
    state_path=state_root / "leases.sqlite3",
    workspace_root=state_root / "workspaces",
)
request = EnvironmentRequest(
    workspace_id=uuid4(),
    agent_id="researcher",
    task_id=uuid4(),
    attempt_id=uuid4(),
    capabilities=frozenset({"python"}),
    os="linux",
)
plan = manager.plan(request, environment_id="python-worker")
print(plan.model_dump_json(indent=2))
```

Use actual workspace, task, and attempt IDs from the authorized assignment in
production. Persist the attempt ID before allocation. Reusing it returns the
existing lease rather than creating another resource; inspect its status before
continuing. A different request cannot take over that attempt.

## Prepare Docker and run an assignment

Provision Docker and the image separately. Images need `/bin/sleep` and the
applications used by commands. Allocation starts an idle container; it does not
install applications, mount company libraries, inject API keys, or launch an
agent reasoning loop. Windows and macOS application sessions use machine runners.

The process creating workspace directories must use ownership/ACLs compatible
with the configured container UID/GID, both defaulting to `1000`. On Linux, run
the manager under matching IDs or provision appropriate group/default ACL access
on the workspace root. The manager does not `chown` directories. Test Docker
Desktop bind-mount permissions before enabling production assignments.

Containers use a read-only root, one dedicated writable `/workspace` mount,
bounded temporary storage, CPU/memory/process limits, no extra Linux capabilities,
and no privilege escalation. No Docker socket or whole-repository mount is
provided. Network access defaults to `none`; `bridge` is an explicit configuration
choice. GPU access requires specific numeric device indexes. Containers share
the host kernel; use dedicated machines when the workload requires a stronger
host boundary.

This function performs real operations only when called after provisioning:

```python
def run_example() -> None:
    lease = manager.allocate(request, environment_id="python-worker")
    if lease.status != "active":
        raise RuntimeError(f"Reconcile lease {lease.id}: {lease.status}")
    ownership = {
        "attempt_id": request.attempt_id,
        "fencing_token": lease.fencing_token,
    }
    print("lease", lease.id, "fence", lease.fencing_token)
    try:
        manager.heartbeat(lease.id, **ownership)
        result = manager.execute(
            lease.id,
            ExecutionCommand(
                argv=("python", "-c", "print('isolated worker ready')"),
                timeout_seconds=30,
                max_output_bytes=4096,
            ),
            **ownership,
        )
        print(result.model_dump_json())
    finally:
        manager.release(lease.id, **ownership)
```

Commands pass explicit arguments without host shell interpolation. Results contain
`exit_code`, `stdout`, `stderr`, and `truncated`. Long-lived render supervision and
artifact promotion require higher-level workers. Release verifies ownership,
removes the container, and retains workspace files for artifact collection.

## Register an existing dedicated machine

A machine definition uses `kind="machine"`, its operating system and capabilities,
`runner_url`, and `credential_env` naming an environment variable containing the
Bearer token. HTTPS is required except for loopback HTTP. `max_concurrency` must
remain `1`; separate simultaneously active agents need separate machine runners.

The implemented client sends `POST /v1/leases`, followed by
`POST /v1/leases/{lease_id}/heartbeat`, `/execute`, or `/release`. Bodies include
`lease_id`, `fencing_token`, the workspace/agent/task/attempt request, and resource
policy. Execution adds `command`. Allocation replies include the matching lease
and fence plus `resource_handle`; execution replies add `result`. All replies
must echo the matching identity.

An external runner must enforce exclusivity, workspace separation, resource and
network policy, command timeout/output limits, and stale-token rejection. It must
remember released leases and make allocation/release idempotent. Merely declaring
a machine in configuration does not establish those guarantees. The remote
daemon, VM/cloud provisioning, application licensing, and desktop-session setup
are not implemented here. See the exact contract in
[`execution_backends.py`](../../src/simon/adapters/execution_backends.py).

## Persistence and interrupted work

Business assignments and their environment plans belong in Simon's existing
database. The SQLite file is a separate resource-ownership journal for one
execution-manager host. Local processes can share it; do not place it on a
network filesystem or treat it as a distributed scheduler. Retain it across
restarts and keep all allocation paths for that host on the same journal.

Unknown outcomes retain their capacity reservation. No command is automatically
replayed. A Docker CLI timeout does not prove the container command stopped.
Inspect the lease with `manager.get(lease_id)` and reconcile or release it before
assigning replacement work. Heartbeat age alone never frees a resource.

After a manager crash, first stop the original manager process. An administrator
can then call `release(..., recover_interrupted=True)` with the recorded attempt
ID and fencing token to clean an interrupted operation. Preserve generated files
and inspect uncertain external effects before creating a new attempt.
