# Local operation

Use the checked-in startup scripts with a configured PostgreSQL service for persistent
development. `/health/live` reports API liveness, and `/auth/config` reports
available login methods. Liveness is not a database readiness check. Use the configured
public origin consistently for sessions and passkeys.

The API serves **Chat**, **Projects**, account access and connections. Native project
staffing is stored in PostgreSQL; adding an agent does not start a worker process.
The optional [background chat worker](assistant.md) is a separate process. Generic
runtime adapters are [libraries](runtime-adapters.md), not an autonomous dispatcher.

```powershell
.\scripts\start-dev.ps1 -DevelopmentLogin
.\scripts\start-assistant-worker.ps1 -Check
```

For an enrolled persistent installation, `start-local.ps1 -Check` validates configured
PostgreSQL and administrator-workspace settings without contacting the database or model.
`start-local.ps1` preserves the configured database, identity, origin and model, waits
for that database, applies migrations and checks that the configured administrator can
sign in. Explicit `-DatabaseUrl`, `-WorkspaceId` and `-ActorId` override only that invocation.
`-DatabaseOnly` separately starts the repository's Compose PostgreSQL service; normal
startup does not assume that a configured database is local or managed by Docker.

Review each script's help and settings before using it on a persistent installation.
Process configuration uses `SIMON_` names. Startup must not override credentials,
current account identity or database targets with retired aliases. A memory backend
is for disposable tests and loses state on restart.

Windows tasks and deployed services are operator-managed. This repository does not
assert which tasks are currently registered, which user is signed in, or whether a
public tunnel is active. Source edits do not modify those host settings. Inspect and
deliberately replace stale installed commands before deploying the rewritten platform.
Use graceful stop/drain procedures and tested [recovery bundles](storage-recovery.md).
