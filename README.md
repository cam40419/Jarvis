# Jarvis

Jarvis is a personal assistant control plane paired with the separately deployable Home OS
physical-automation subsystem. The repository is being rebuilt from first principles; the legacy
runtime remains recoverable from Git history but is no longer part of the working tree.

## Current state

- Passkey enrollment and sign-in, persistent sessions, logout, and local recovery tools
- Server-resolved household permissions, session-bound CSRF checks, and secure cookie settings
- A browser sign-in page with an authenticated connection test
- Real OpenAI Responses API answers with bounded context and shared household memories
- Persistent conversations, immutable run snapshots, and resumable run event streams
- Bounded context selection, source-linked excerpts, and explicit shared household memories
- One read capability, `system.echo`, with validated and idempotent invocation
- Persistent jobs with optimistic version checks and household-scoped lookup
- Household audit chains and transactional outbox delivery intents
- Versioned PostgreSQL/pgvector migrations and backup/restore verification
- Shared memory/PostgreSQL tests, real WebAuthn signature tests, a browser ceremony test, and CI

The launcher starts a real text assistant for questions, writing, planning, and code.
Calendar, email, live web access, job execution workers, and physical-device integration are not enabled.
Submitted jobs remain queued. Hard and dangerous writes remain blocked; passkey sign-in does
not substitute for action-bound confirmation.

## Run locally

Use Python 3.11 or newer and start Docker Desktop. From the repository root in PowerShell:

```powershell
# Create venv only if it does not already exist.
py -3.11 -m venv venv
.\venv\Scripts\python.exe -m pip install -e ".[dev,postgres]"
# Set JARVIS_OPENAI_API_KEY in .env first. Keep this file private.
.\scripts\start-dev.ps1 -Enroll
```

Open **http://localhost:8000/login**, expand **Set up a passkey**, and use the enrollment token
printed by the launcher. Complete the browser/device prompt. Try **Test connection**, **Sign out**,
and **Sign in with a passkey**. For subsequent launches use `.\scripts\start-dev.ps1`.

Select **Open conversations** after signing in. Create a conversation, send a message, and reload
to verify that both messages persist. See the [conversation runbook](docs/runbooks/conversations.md)
for restart and event-stream reconnection checks.
Expand **Shared household memories** to save a fact, send a message, then inspect **Last run context**.
The [context runbook](docs/runbooks/context.md) explains selection, budgets, and retraction.

To try the session flow without a passkey, use `.\scripts\start-dev.ps1 -DevelopmentLogin`
and enter its printed token under **Local development login**. For a database-free smoke test,
`.\scripts\start-dev.ps1 -Memory` enables temporary development login and loses state on restart.

The launcher initializes the local database and development membership without editing `.env`.
The standard password matches Compose's local default; use `-DatabaseUrl` for a custom connection.
Use `localhost` consistently: `127.0.0.1` is a different browser origin.
The launcher defaults to OpenAI using `JARVIS_OPENAI_API_KEY`; requests use your API billing.
Use `-TestRunner` for the offline echo path, which needs no API key.
See [using the assistant](docs/runbooks/assistant.md) for examples and troubleshooting.

**The old identity headers no longer authenticate requests.** APIs use a session cookie, and
protected POSTs require the matching Origin and `X-CSRF-Token`. See the
[identity runbook](docs/runbooks/identity.md) for browser/API tests, membership management,
enrollment, revocation, and configuration details. The API schema is at `/docs`.

## Verify

```powershell
.\venv\Scripts\python.exe -m ruff check src tests scripts
.\venv\Scripts\python.exe -m mypy src
.\venv\Scripts\python.exe -m pytest -q -m "not postgres"
```

For the full suite, create the disposable test database once:

```powershell
docker compose -f deploy/compose/compose.yaml exec -T postgres createdb -U jarvis jarvis_test
$env:JARVIS_TEST_DATABASE_URL = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis_test'
.\venv\Scripts\python.exe -m pytest --cov=jarvis --cov-report=term-missing
```

Skip `createdb` if it already exists. PostgreSQL tests isolate each run in temporary schemas.
The optional real-browser test and its installation instructions are in the identity runbook;
CI runs it with Chromium. The coverage gate is 90% for the combined storage suite.

See the [persistence runbook](docs/runbooks/persistence.md) for backup/restore and migration
behavior, the [target architecture](docs/architecture/target-architecture.md), architecture
decisions under [`docs/decisions`](docs/decisions), and the
[Phase 1 readiness checklist](docs/phase-1-readiness.md). Tool integration is the next milestone.
