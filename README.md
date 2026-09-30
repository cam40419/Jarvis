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
The primary app runs on this PC at **http://localhost:8000/login**. The former website route is
paused while remote access is redesigned as a thin route to this PC; the tunnel is stopped.
See the [remote and voice runbook](docs/runbooks/remote-voice.md) for the previous deployment and
the local-first direction.
See the [next phases](docs/next-phases.md) for the remaining rollout, scenes, and schedules.

Account invitations are under **Account & access &rarr; Accounts**. Choose **Personality & voice**
in chat to edit Simon's manner and voice. The [accounts and workshop plan](docs/accounts-workshop-plan.md)
covers filesystem tools, the Bambu A1, your plate-swap generator, and the proposed app layout.

The [workflow foundation](docs/workflow-architecture.md) now supports durable runs, timed steps,
dependencies, recovery, and API controls. Start its separate read-only worker with
`.\scripts\start-workflow-worker.ps1`; the guide includes a working demo. Chat/UI integration,
condition monitoring, recurring schedules, and device writes are next.

## Current state

- Username/password and passkey sign-in, persistent sessions, logout, and local recovery tools
- Administrator-issued account invitations, private workspaces, and account disable/re-enable
- Personal personality/voice settings, including Jarvis mode and Vesper voice
- Server-resolved household permissions, session-bound CSRF checks, and secure cookie settings
- A browser sign-in page with an authenticated connection test
- Real OpenAI Responses API answers with bounded context and personal/shared memories
- Automatic model, reasoning effort, and answer length; streaming, Stop, and Think deeper
- A responsive chat interface with searchable history, Markdown/code, copy, themes, and memory controls
- Saved personal response defaults, an automatic Deep preference, and persistent answer feedback
- Public web search with saved citations and source links
- Google account connection, primary calendar reads, and confirmed email/event previews
- Automatic LIFX/Tuya discovery, persistent rooms/groups organized in chat, and immediate power/brightness/color controls
- Persistent device names in chat, Shelly Gen4 LAN discovery and chat outlet setup/control
- Outlet name, room, load type, and immediate on/off controls in Connections
- Backend power/energy monitoring with persistent meter history
- A Home dashboard with room cards, immediate light/outlet controls, and Shelly power charts
- Raspberry Pi idle displays with uploaded slideshows, live widgets, and chat-managed layouts
- Live browser voice with captions, mute, interruption support, backend task cancellation, and call history
- Configurable `/simon` hosting, private passkey sign-in, and Vercel/home-tunnel deployment examples
- Persistent conversations, immutable run snapshots, and resumable run event streams
- Shared recall across text and voice; conversational saving of personal facts, preferences, and projects
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

On this PC, Simon starts when `cam40` signs in to Windows. From the repository root in PowerShell:

```powershell
Start-ScheduledTask -TaskName Simon-PostgreSQL
Start-ScheduledTask -TaskName Simon-Local
Start-ScheduledTask -TaskName Simon-Workflow
```

Open **http://localhost:8000/login** and sign in as `cam40419` with the existing password. The
server uses PostgreSQL and one Uvicorn worker, with development-token login disabled. New installs
need Python 3.11 or newer, Docker Desktop, a virtual environment with `.[dev,postgres]`, and a
private `.env` with `SIMON_OPENAI_API_KEY`; run `.\scripts\install-local-tasks.ps1` after account
setup. See [local operations](docs/runbooks/local-operations.md) for startup, recovery, logs,
graceful shutdown, and backups.

Select **Open home dashboard** after signing in. The Home tab shows connected devices by room,
their current status, power readings, and recent commands. Outlet and light status is checked
on load, every 30 seconds while the page is visible, and when you return to it. Lights have direct power, brightness,
and color controls; outlets can be configured from their device cards. The Simon tab has Chat,
Work, and Voice entry points. Work shows project context and read only workflows.

Open **Displays** to provision a Raspberry Pi, upload slideshow images, and choose its layout.
The Pi setup and kiosk/autostart instructions are in the
[idle display runbook](docs/runbooks/idle-display.md). After provisioning, ask Simon things like
“put power in the top right and the printer in the bottom right” or “switch the kitchen display
to split layout”; the screen picks up changes automatically.

In Simon, send a message to start a conversation, and reload
to verify that both messages persist. See the [conversation runbook](docs/runbooks/conversations.md)
for restart and event-stream reconnection checks.
Open **Memory** in the sidebar to review saved facts, preferences, and projects. New entries default
to personal; you can explicitly share a manual entry with the workspace. Simon can also save
durable context as you talk and search your earlier text/voice conversations. Start a new chat or
voice call to use private recall; older shared threads retain their existing access.
Expand the response's small run label to
inspect its selected context, model, reasoning, and usage.
The [context runbook](docs/runbooks/shared-context.md) explains selection, budgets, and retraction.
Leave the composer on **Auto** and ask a question. Simon chooses the model, reasoning effort, and
answer length. The Auto menu provides optional overrides. **Stop** cancels an unfinished answer;
**Think deeper** makes a new, linked Deep response. The [model management plan](docs/model-management-plan.md)
tracks spending controls, feedback, and adaptive routing planned after these controls.

Invited users can use a one-use enrollment code to create a username and password or a passkey.
Passwords are stored as Argon2id hashes. The development launcher remains available for isolated
tests, but `start-local.ps1` is the normal launcher for this PC. Use `localhost` consistently:
`127.0.0.1` is a different browser origin. OpenAI requests use your API billing.
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
