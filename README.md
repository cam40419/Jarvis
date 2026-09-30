# Simon

**SIMON: Smart Interactive Memory and Orchestration Network.** This repository remains named
Jarvis and owns the personal AI assistant: models, chat, voice, memory, projects, files,
Google connections, background AI tasks, and work sessions.

Home control runs independently in [RobbinsHome](https://github.com/cam40419/RobbinsHome).
Simon has no device drivers, LAN discovery, home storage access, printer controls, display
server, or home automation worker. The optional integration is an authenticated HTTP tool
client. Neither repository needs the other's Python package or source checkout.

## Run locally

Use Python 3.11+ and a local virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -e '.[dev,postgres]'
```

Configure `.env` using [.env.example](.env.example). Set `SIMON_OPENAI_API_KEY` for OpenAI
mode; `JARVIS_OPENAI_API_KEY` and bare `OPENAI_API_KEY` are not used. For existing local
installations, `.\scripts\start-local.ps1` starts the database and assistant. Open
**http://localhost:8000/login**, then Chat or Work. Run AI background work separately with
`.\scripts\start-assistant-worker.ps1`. Existing `Simon-Workflow` scheduled tasks continue
through a compatibility launcher; they now execute only assistant tasks and work sessions.

New installations can use `.\scripts\start-dev.ps1 -Memory -TestRunner` for an isolated
local smoke run, or follow the [identity setup](docs/runbooks/identity.md) and
[local operations](docs/runbooks/local-operations.md) guides for persistent accounts.
`SIMON_ACCOUNT_HOUSEHOLD_ID` identifies the administrator's workspace in launcher checks.

## Optional RobbinsHome tools

Configure `SIMON_HOME_API_URL` (for example `http://localhost:8001`) and
`SIMON_HOME_API_TOKEN`. RobbinsHome independently grants the token access to explicit actor /
household IDs and scopes, intersected with its current memberships. Home provider credentials
belong exclusively in RobbinsHome. An unavailable home server does not prevent normal chat,
files, projects, or Google tools from working. Connections links to the separate home UI.

See [repository separation and cutover](docs/architecture/repository-separation.md) for
migration, authentication, rollback, and the independent deployment requirements.

## Features and guides

- [Text assistant](docs/runbooks/assistant.md), [conversations](docs/runbooks/conversations.md),
  [memory and context](docs/runbooks/shared-context.md), and [voice](docs/runbooks/remote-voice.md)
- [Google connections](docs/runbooks/google.md), [local files](docs/runbooks/local-files.md),
  personal projects, background tasks, and work sessions
- [Accounts and identity](docs/runbooks/identity.md), [persistence](docs/runbooks/persistence.md),
  and [local operations](docs/runbooks/local-operations.md)
- [Configurable multi-agent platform plan](docs/architecture/multi-agent-company-platform-plan.md):
  task, project, and company teams; creative/engineering tools; concurrency, artifacts, and rollout
- [Agent platform foundation](docs/runbooks/agent-platform.md): implemented planning API,
  extensible tools, isolated container/machine execution interfaces, and local/API model routing
- [Agent profiles](docs/runbooks/agent-profiles.md) and [team execution](docs/runbooks/agent-execution.md):
  configurable prompts and limits, concurrent workers, cancellation, artifacts, and recovery

Older planning documents describe the former combined system. Current home runbooks,
examples, dashboard, displays, printer batches, and automation schedules live in RobbinsHome.
Historical SQL migrations remain unchanged for existing installations and rollback; the
assistant runtime no longer reads or writes the legacy home/automation tables.

## Verify

```powershell
.\venv\Scripts\python.exe -m ruff check src tests scripts
.\venv\Scripts\python.exe -m mypy src
.\venv\Scripts\python.exe -m pytest -q -m "not postgres and not browser and not live"
```

PostgreSQL tests require `SIMON_TEST_DATABASE_URL` pointing to a disposable database ending
in `_test`. Browser tests require `SIMON_BROWSER_TESTS=1` and Playwright Chromium. Live model
checks require explicit opt-in and use API billing.
