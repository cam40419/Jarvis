# Simon

**SIMON: Smart Interactive Memory and Orchestration Network.** The repository remains named
Jarvis. The application, Python package, CLI commands, and new configuration use Simon / `simon` /
`SIMON_`. The OpenAI key must use `SIMON_OPENAI_API_KEY`; legacy `JARVIS_OPENAI_API_KEY` and
bare `OPENAI_API_KEY` are not used. Other existing `JARVIS_` settings remain accepted.
Existing database/volume names and historical records stay intact to preserve your data.

Simon is a personal assistant control plane paired with the separately deployable Home OS
physical-automation subsystem. The repository is being rebuilt from first principles; the legacy
runtime remains recoverable from Git history but is no longer part of the working tree.

Try voice with **Talk to Simon** in chat, and open **Home** for device controls and power charts.
For phone access at `camrobbins.com/simon`, follow the [remote and voice setup](docs/runbooks/remote-voice.md).
The public route still needs its tunnel and Vercel portfolio configuration deployed.
See the [next phases](docs/next-phases.md) for the remaining rollout, scenes, and schedules.

## Current state

- Passkey enrollment and sign-in, persistent sessions, logout, and local recovery tools
- Server-resolved household permissions, session-bound CSRF checks, and secure cookie settings
- A browser sign-in page with an authenticated connection test
- Real OpenAI Responses API answers with bounded context and shared household memories
- Automatic model, reasoning effort, and answer length; streaming, Stop, and Think deeper
- A responsive chat interface with searchable history, Markdown/code, copy, themes, and shared memory
- Saved personal response defaults, an automatic Deep preference, and persistent answer feedback
- Public web search with saved citations and source links
- Google account connection, primary calendar reads, and confirmed email/event previews
- Automatic LIFX/Tuya discovery, persistent rooms/groups organized in chat, and immediate power/brightness/color controls
- Persistent device names in chat, Shelly Gen4 LAN discovery and chat outlet setup/control
- Outlet name, room, load type, and immediate on/off controls in Connections
- Backend power/energy monitoring with persistent meter history
- A Home dashboard with room cards, immediate light/outlet controls, and Shelly power charts
- Live browser voice with captions, mute, interruption support, backend task cancellation, and call history
- Configurable `/simon` hosting, private passkey sign-in, and Vercel/home-tunnel deployment examples
- Persistent conversations, immutable run snapshots, and resumable run event streams
- Bounded context selection, source-linked excerpts, and explicit shared household memories
- An idempotent `system.echo` health capability and a bounded connected-tool runtime
- Persistent jobs with optimistic version checks and household-scoped lookup
- Household audit chains and transactional outbox delivery intents
- Versioned PostgreSQL/pgvector migrations and backup/restore verification
- Shared memory/PostgreSQL tests, real WebAuthn signature tests, a browser ceremony test, and CI

The launcher starts a real text assistant for questions, writing, planning, and code.
Public web search is enabled in OpenAI mode. Connect Google to read your primary calendar and
confirm email sends or new events in chat; see [Google setup](docs/runbooks/google.md). Reservation
search and email requests are supported. LIFX and linked Tuya devices appear automatically once
their credentials are set; ask Simon to name devices, assign rooms/groups, or check status. See
[home device setup](docs/runbooks/home-devices.md) for credentials and Shelly outlet setup in chat.
Home commands execute directly: try ?All off? or ?Make the office lights blue at 40%.?
Device results show accepted, verified, failed, or unknown outcomes; physical changes have not
been exercised during development. Tuya registration requires populated project credentials.
Website booking forms, scheduled device routines, and job workers remain future work.
Submitted jobs remain queued. Hard and dangerous writes remain blocked; passkey sign-in does
not substitute for action-bound confirmation.

## Run locally

Use Python 3.11 or newer and start Docker Desktop. From the repository root in PowerShell:

```powershell
# Create venv only if it does not already exist.
py -3.11 -m venv venv
.\venv\Scripts\python.exe -m pip install -e ".[dev,postgres]"
# Set SIMON_OPENAI_API_KEY in .env first. Keep this file private.
.\scripts\start-dev.ps1 -Enroll
```

Open **http://localhost:8000/login**, expand **Set up a passkey**, and use the enrollment token
printed by the launcher. Complete the browser/device prompt. Try **Test connection**, **Sign out**,
and **Sign in with a passkey**. For subsequent launches use `.\scripts\start-dev.ps1`.

Select **Open conversations** after signing in. Send a message to start a conversation, and reload
to verify that both messages persist. See the [conversation runbook](docs/runbooks/conversations.md)
for restart and event-stream reconnection checks.
Open **Shared memories** in the sidebar to save a fact. Expand the response's small run label to
inspect its selected context, model, reasoning, and usage.
The [context runbook](docs/runbooks/context.md) explains selection, budgets, and retraction.
Leave the composer on **Auto** and ask a question. Simon chooses the model, reasoning effort, and
answer length. The Auto menu provides optional overrides. **Stop** cancels an unfinished answer;
**Think deeper** makes a new, linked Deep response. The [model management plan](docs/model-management-plan.md)
tracks spending controls, feedback, and adaptive routing planned after these controls.

To try the session flow without a passkey, use `.\scripts\start-dev.ps1 -DevelopmentLogin`
and enter its printed token under **Local development login**. For a database-free smoke test,
`.\scripts\start-dev.ps1 -Memory` enables temporary development login and loses state on restart.

The launcher initializes the local database and development membership without editing `.env`.
The standard password matches Compose's local default; use `-DatabaseUrl` for a custom connection.
Use `localhost` consistently: `127.0.0.1` is a different browser origin.
The launcher defaults to OpenAI using `SIMON_OPENAI_API_KEY`; requests use your API billing.
Use `-TestRunner` for the offline echo path, which needs no API key.
The development launcher watches `.env` and restarts the app when it changes. Restart the launcher
once to pick up this behavior if it was already running. A process-level `SIMON_OPENAI_API_KEY`
overrides `.env`; remove or update that process variable if changing the file has no effect.
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
$env:SIMON_TEST_DATABASE_URL = 'postgresql://jarvis:local-development-only@127.0.0.1:5432/jarvis_test'
.\venv\Scripts\python.exe -m pytest --cov=simon --cov-report=term-missing
```

Skip `createdb` if it already exists. PostgreSQL tests isolate each run in temporary schemas.
The optional real-browser test and its installation instructions are in the identity runbook;
CI runs it with Chromium. The coverage gate is 90% for the combined storage suite.

See the [persistence runbook](docs/runbooks/persistence.md) for backup/restore and migration
behavior, the [target architecture](docs/architecture/target-architecture.md), architecture
decisions under [`docs/decisions`](docs/decisions), and the
[Phase 1 readiness checklist](docs/phase-1-readiness.md). The
[home integration plan](docs/home-integration-plan.md) tracks device commissioning and routines.
