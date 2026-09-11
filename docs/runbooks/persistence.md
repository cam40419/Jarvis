# Local persistence and verification

Run all commands in PowerShell from the repository root. Use Python 3.11 or newer;
CI uses 3.11. The local verification on 2026-09-10 used Python 3.13.7.

## Start the persistent API

Identity now uses authenticated sessions; the old actor/scope headers no longer work.
Start Docker Desktop, then:

```powershell
.\venv\Scripts\python.exe -m pip install -e ".[dev,postgres]"
.\scripts\start-dev.ps1 -DevelopmentLogin
```

For passkey sign-in, use `-Enroll` and follow the [identity runbook](identity.md).
The launcher runs migrations and seeds the explicit development membership without changing `.env`.
Use `-DatabaseUrl` for a custom database password/connection. `/health/live` is process liveness,
not database readiness. Open http://localhost:8000/login. The API offers echo, job submission,
and job lookup. Use `localhost` consistently for the browser origin.

## Test persistence manually

In another PowerShell terminal:

```powershell
$token = Read-Host 'Development token printed by the launcher'
$login = Invoke-RestMethod -Uri 'http://localhost:8000/auth/dev-login' `
    -Method Post -SessionVariable jarvisSession -ContentType 'application/json' `
    -Headers @{ Origin = 'http://localhost:8000' } `
    -Body (@{ token = $token } | ConvertTo-Json)
$token = $null
$headers = @{ Origin = 'http://localhost:8000'; 'X-CSRF-Token' = $login.csrf_token }
$key = [guid]::NewGuid().ToString()
$echoBody = @{
    capability = "system.echo"
    arguments = @{ message = "Hello from persistent Jarvis" }
    idempotency_key = $key
} | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8000/v1/capabilities/invoke" `
    -Method Post -WebSession $jarvisSession -Headers $headers `
    -ContentType "application/json" -Body $echoBody

$jobBody = @{
    kind = "test.job"
    input = @{ message = "Keep this across restart" }
    idempotency_key = $key
} | ConvertTo-Json
$job = Invoke-RestMethod -Uri "http://localhost:8000/v1/jobs" `
    -Method Post -WebSession $jarvisSession -Headers $headers `
    -ContentType "application/json" -Body $jobBody
$job
```

Stop the API with Ctrl+C and restart `.\scripts\start-dev.ps1 -DevelopmentLogin`.
Keep the second terminal open, then:

```powershell
Invoke-RestMethod -Uri "http://localhost:8000/v1/jobs/$($job.id)" -WebSession $jarvisSession
Invoke-RestMethod -Uri "http://localhost:8000/v1/capabilities/invoke" `
    -Method Post -WebSession $jarvisSession -Headers $headers `
    -ContentType "application/json" -Body $echoBody
```

The job retains its ID and input. Echo returns `replayed: true` with the original invocation ID
and completion time. Reposting the original job returns the same job. Changing input with the
same idempotency key returns 409. A job kind such as `!` returns 422. Missing required scopes
return 403. Job lookup with an authorized different household returns 404; an unknown database
membership is rejected with 403 before lookup.

Jobs remain `queued`: a worker/lease execution service is a later milestone. Sessions persist
until their absolute expiry; disabling development login rejects development sessions immediately.

## Automated verification

Quick tests, without a database:

```powershell
.\venv\Scripts\python.exe -m pytest -q -m "not postgres"
```

Full suite and coverage (create `jarvis_test` once):

```powershell
docker compose -f deploy/compose/compose.yaml exec -T postgres createdb -U jarvis jarvis_test
$env:JARVIS_TEST_DATABASE_URL = "postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis_test"
.\venv\Scripts\python.exe -m ruff check src tests scripts
.\venv\Scripts\python.exe -m mypy src
.\venv\Scripts\python.exe -m pytest --cov=jarvis --cov-report=term-missing
```

If `jarvis_test` already exists, skip `createdb`. Tests require the database name to end in
`_test`; each test creates and removes its own randomly named schema. Never point this variable
at the development or production database. A configured but unreachable test database fails
the run; it does not silently skip PostgreSQL verification.

The same storage scenarios run against memory and PostgreSQL: concurrent duplicates, distinct
concurrent audit writes, optimistic versions, household lookup isolation, rollback of state/audit/
outbox, nested savepoints, stable invocation replays, and competing outbox publishers. Additional
PostgreSQL tests cover migrations, membership checks, and an actual API process restart.
CI provisions pgvector/PostgreSQL and runs the full suite with a 90% coverage gate.

## Backup and restore drill

Stop API/worker writers while running this consistency comparison. Leave PostgreSQL running.

```powershell
.\venv\Scripts\python.exe scripts/verify_restore.py
```

The script reads the `jarvis` database, writes a binary `pg_dump` archive under `.local/backups`,
restores it into a uniquely named temporary database, compares every public table's complete row
contents, and removes only that temporary database. It verifies that the source did not change
throughout the drill. The archive and JSON report remain on disk and are ignored by Git. The
report contains the archive checksum and per-table hashes, not the table contents. The backup
itself contains the database contents. Python handles binary I/O to avoid PowerShell redirection
changing the archive bytes.

To inspect an archive or perform a manual recovery, copy the selected `.dump` file into the
container and restore into a new, empty database, keeping the original intact:

```powershell
# Substitute the actual archive path printed by the verification script.
docker compose -f deploy/compose/compose.yaml cp .local/backups/ARCHIVE.dump postgres:/tmp/jarvis.dump
docker compose -f deploy/compose/compose.yaml exec -T postgres createdb -U jarvis jarvis_recovered
docker compose -f deploy/compose/compose.yaml exec -T postgres pg_restore -U jarvis `
    -d jarvis_recovered --exit-on-error --no-owner --no-privileges /tmp/jarvis.dump
```

Switch the API connection URL to `jarvis_recovered` after verifying its data. This drill tests
logical recovery on the local PostgreSQL version; off-machine backup storage, retention,
encryption, point-in-time recovery, and disaster recovery timing are not implemented.

## Migration behavior

`python -m jarvis.migrate` serializes migration runs with a database advisory lock. SQL changes
and checksum records commit in one transaction. Reruns skip unchanged versions, reject changed
or unknown versions, and roll back failed migrations. Do not edit applied migrations: add a
new numbered `.sql` file. `.down.sql` files are not automatically executed. SQL files ship in
the Python wheel as well as the source checkout.

A database initialized by the old Compose SQL mount has no migration version record. The
runner intentionally refuses to silently adopt existing tables. Preserve/backup that database
and initialize a separate empty database for this version; no automatic destructive reset or
legacy-schema adoption is provided.

## Transaction and delivery limits

- Job changes, successful invocation replay records, audit entries, and pending outbox events
  share a transaction. Audit chains are scoped to a household. Household writes are serialized
  initially to keep audit ordering deterministic.
- Capability handlers and schemas remain a process-local code registry. Only `system.echo` is
  enabled. PostgreSQL transactions cannot roll back an external API/device action; no such
  live side effect is introduced here.
- `publish_pending(deliver, limit)` claims unpublished events with `FOR UPDATE SKIP LOCKED`,
  tracks attempts, and marks successful delivery. Failed callbacks leave an event pending.
  Delivery is at least once: consumers must deduplicate by event ID because a process can
  crash after delivery and before its database commit. Callbacks must be bounded and must not
  call back into the Jarvis store. No external consumer or notification sender is enabled.
- Each synchronous transaction opens its own connection. Connection pooling, worker leases,
  persistent capability administration, rate limiting, and a chat interface remain future work.
  Passkeys, sessions, and server-resolved membership scopes are implemented; see the identity runbook.
