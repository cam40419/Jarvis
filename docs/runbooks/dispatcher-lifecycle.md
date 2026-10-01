# Windows dispatcher startup and recovery

The API and agent dispatcher use the same PostgreSQL database, manifest and
`SIMON_AGENT_STATE_DIR` on one manager host. Use the configured launchers to
preserve the operator's `.env` settings:

```powershell
.\scripts\start-configured-server.ps1 -Check
.\scripts\start-agent-dispatcher.ps1 -Check
```

Checks validate configuration without opening a database connection, starting
workers, running migrations or registering tasks. They do not establish provider,
database, Docker or network availability. Invalid configuration values are
redacted. Normal startup waits up to five minutes for the configured PostgreSQL database,
honoring stop/maintenance markers, then applies migrations; the migration runner serializes
concurrent callers with a PostgreSQL advisory lock.

The server launcher records `.local/logs/configured-server.log`. The agent launcher
records `.local/logs/agent-dispatcher.log`, including readiness, migration, worker startup,
shutdown, and redacted error-class diagnostics. The agent log rotates at 5 MiB with
three backups; the server transcript should be rotated during maintenance.
Keep log access restricted to the server operator. `scripts/run_hidden.py` preserves
the child process exit code so
Task Scheduler can detect failures.

```powershell
.\scripts\install-agent-task.ps1 -Start
```

This explicitly registers `Simon-Agents` for the current user's sign-in session.
It permits one scheduled instance, has no execution-time limit, and retries a
failed exit three times at one-minute intervals. Scheduled starts use `-Supervised`, preserving
intentional stops across logon and automatic retries. It does not run before sign-in;
Docker Desktop and PostgreSQL must be available. Existing failed or interrupted
runs are not automatically replayed when the task restarts.

To request a graceful dispatcher stop from the repository directory:

```powershell
New-Item -ItemType File -Path .local/agent-dispatcher-stop.request -Force | Out-Null
Get-ScheduledTask -TaskName Simon-Agents | Select-Object TaskName, State
```

The continuous launcher checks this request each polling interval, stops claiming
new runs, drains work already claimed, closes the database pool and exits
successfully. Wait for the scheduled task to leave `Running` before maintenance.
An active run waiting on another environment may need cancellation through its
run controls before it can finish draining. Cancellation does not reverse writes
already accepted by external services. The request remains present so recovery
supervision recognizes the intentional stop. Run `.\scripts\resume-local.ps1 -Service agents`
to resume the registered dispatcher. A direct non-supervised PowerShell launch
clears it before configuration checks and migrations, preserving any new request
that arrives during setup. The API has a separate `.local/simon-stop.request` file
with the same behavior. Direct Python entrypoints honor existing requests;
operators must remove a previous request themselves when restarting without a
launcher.

For maintenance across services, create `.local/maintenance.request` before
requesting each service's graceful stop. Recovery and configured launchers leave
services stopped while that marker exists; `-Check` still works. Remove the
maintenance marker when ready, then use `resume-local.ps1` for the desired services.

Direct console dispatchers also drain on Ctrl+C, Ctrl+Break on Windows, or SIGTERM
where supported. Outside these launchers, opt into the file control using
`python -m simon.agent_dispatcher --stop-file <operator-owned-path>`.
`--stop-file` applies only to continuous mode, not `--once` or recovery.

For a forcibly terminated dispatcher, first stop its old process and owned
workers. Inspect the saved run and external side effects, then use its current
version in the explicit recovery command:

```powershell
venv/Scripts/python.exe -m simon.agent_dispatcher --recover-run <run-uuid> --expected-version <version> --worker-stopped
```

Recovery changes running tasks to unknown outcomes, cancels tasks that had not
started, and marks the run for human review. It does not replay uncertain writes.
After confirming cleanup, resume `Simon-Agents` with `resume-local.ps1 -Service agents`
for newly queued work.

Local file reads used by imports and previews validate the actual opened file.
POSIX reads pin ancestor directory handles; Windows reads reject reparse leaves
and verify the handle's resolved path before reading bytes. The latter follows
the documented behavior of
[GetFinalPathNameByHandleW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew).
Filesystem redirects and content that changes during a read are rejected.
