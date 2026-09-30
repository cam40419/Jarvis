# Local Simon operations

Simon runs on this PC at **http://localhost:8000/login**. Sign in as `cam40419` with the existing
password. The API binds to both loopback addresses, `127.0.0.1` and `::1`, so `localhost` connects
quickly whichever address Windows resolves first. Use `localhost` in the browser because that is the
configured origin and passkey relying-party ID. The public tunnel is off.

## What starts at Windows sign-in

Run `./scripts/install-local-tasks.ps1` from PowerShell in the repository root to register the five
tasks below. They run as the current Windows user with an interactive logon token; sign in to Windows
before expecting Simon to be available. Docker Desktop also starts at user sign-in.

| Task | Purpose |
| --- | --- |
| `Simon-PostgreSQL` | At sign-in, wait for Docker Desktop and start the Compose PostgreSQL container. The container itself has `restart: unless-stopped`. |
| `Simon-Local` | At sign-in, apply migrations, check the `cam40419` owner/password credential, and run one Uvicorn server without a reload watcher. Task Scheduler retries failed exits three times. |
| `Simon-Workflow` | At sign-in, run Simon's separate workflow worker for scheduled runs, including enabled home device actions. |
| `Simon-Recovery` | Every five minutes, restart the PostgreSQL task if its container is unhealthy and restart the app task if HTTP health fails and the task has stopped. |
| `Simon-Backup` | At 3 AM, create and format-check a PostgreSQL archive. It retains the newest 14 automatic archives. |

Task Scheduler uses the current user's Windows session because Docker Desktop is a user-session app.
These tasks do not provide service before that user signs in. The recovery task checks liveness; if
the app remains running but hangs, inspect its log and restart it manually.

The task actions use the virtual environment's `pythonw.exe` and `scripts/run_hidden.py` to start
PowerShell with `CREATE_NO_WINDOW`. This keeps the interactive user token required by Docker Desktop
without allowing recurring task consoles to flash on the desktop.

## Operate and check

```powershell
Start-ScheduledTask -TaskName Simon-PostgreSQL
Start-ScheduledTask -TaskName Simon-Local
Start-ScheduledTask -TaskName Simon-Workflow
Invoke-RestMethod http://localhost:8000/health/live
Invoke-RestMethod http://localhost:8000/auth/config
Get-ScheduledTask -TaskName Simon-PostgreSQL,Simon-Local,Simon-Workflow,Simon-Recovery,Simon-Backup |
    Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName Simon-Local
```

`/health/live` should return `{"status":"ok"}`. Auth configuration should report
`dev_login_enabled:false` and `password_enabled:true`. The login page should load at `/login`.
The `cam40419` password credential and seven transferred devices remain in PostgreSQL. The former
development-token session was revoked; development-token login returns HTTP 401. Existing passkey
records remain untouched.

For a controlled shutdown, run `./scripts/stop-local.ps1`. It requests Uvicorn shutdown and waits
for the FastAPI cleanup handlers to finish. Restart with `Start-ScheduledTask -TaskName Simon-Local`.
If recovery stays enabled, it will also restart Simon within five minutes. Logs are under
`.local/logs/simon.log`, `postgres.log`, and `backup.log`; Task Scheduler's `LastTaskResult` is `0`
after a completed successful task and `267009` while the long-running app task is active.

## Backups and recovery

Automatic archives and JSON checksum reports are under `.local/backups/simon_auto_*.dump` and
`.json`. The archives contain account data and credentials, so keep that directory private. They
are on the same PC as the database; copy selected archives to independent storage for protection
against a PC or disk failure. Automatic backups are consistent live `pg_dump` snapshots and pass
`pg_restore --list`. A format check alone does not prove that every row restores correctly.

The full restore drill compares every public table with a temporary restored database. Pause the
recovery task and app writers before running it, leaving PostgreSQL running:

```powershell
Disable-ScheduledTask -TaskName Simon-Recovery
.\scripts\stop-local.ps1
.\venv\Scripts\python.exe scripts/verify_restore.py
Enable-ScheduledTask -TaskName Simon-Recovery
Start-ScheduledTask -TaskName Simon-Local
```

The September 16 drill restored and compared **37 tables** successfully. Its archive and report are
`.local/backups/simon_20260917T031119Z_f896956c.dump` and `.json`. The drill removes only its
temporary database. See [persistence](persistence.md) for the manual restore commands.

After a future Windows reboot, sign in as `cam40`, wait for Docker Desktop, then check the five
tasks, `/health/live`, `/auth/config`, and the `cam40419` login and Home pages. Registration, a
scheduled restart, a controlled shutdown, a stopped-container recovery, and a full restore were
verified on September 16; an actual Windows reboot was not performed during this setup.
