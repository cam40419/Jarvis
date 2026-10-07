# Simon

**SIMON: Somehow It Manages Our Nonsense.**

Simon runs on your local server with a browser workspace for projects, conversations,
files and agent teams. Local PostgreSQL and managed file storage keep the work between
sessions; optional cloud connections provide additional sources and destinations.

In **Work**, create a project, select its team and lead, and describe the outcome you
want. The lead breaks the request into validated specialist tasks and reviews the results.
Manual mode waits for plan approval. Scheduled mode continues toward a standing objective
within saved cadence, cycle and model-budget limits. Todos, findings, activity and outputs
remain attached to the project.

- [Project teams, planning and ongoing work](docs/runbooks/project-teams.md)
- [Current roadmap, response waits and team/agent schedules](docs/project-roadmap.md)
- [ClickUp project boards and company work management](docs/runbooks/project-boards.md)
- [Installed tools and setup](docs/runbooks/work-platform.md)
- [Bookings, orders, reservations and phone messages](docs/runbooks/external-actions.md)
- [Local storage, backup and recovery](docs/runbooks/storage-recovery.md)
- [HTTPS and remote access](docs/runbooks/remote-access.md)

External commitments use a separate review queue. Provider accounts and credentials are
required for live bookings, orders and calls. Available adapters and configured connections
are shown separately in the application.

## Run locally

Use Python 3.11+ and a local virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -e '.[dev,postgres]'
```

Configure `.env` from [.env.example](.env.example). OpenAI mode uses
`SIMON_OPENAI_API_KEY`. For an existing local installation, start the database and
assistant with `.\scripts\start-local.ps1`, then open **http://localhost:8000/login**.
Background conversations and work sessions use `.\scripts\start-assistant-worker.ps1`.
Project agent teams and recurring work use `.\scripts\start-agent-dispatcher.ps1`.
The API alone does not execute queued project work.

New installations can use `.\scripts\start-dev.ps1 -Memory -TestRunner` for an isolated
smoke run, or follow [identity setup](docs/runbooks/identity.md) and
[local operations](docs/runbooks/local-operations.md) for persistent accounts.

## Other guides

- [Current roadmap and delivery order](docs/next-phases.md)
- [Native shared-project API foundation](docs/runbooks/native-projects.md)
- [Autonomous work platform proposal](docs/architecture/autonomous-work-platform-plan.md)
- [Integration and deliverable production plan](docs/architecture/integration-delivery-plan.md)
- [Clothing-brand pilot and core acceptance](docs/architecture/clothing-brand-pilot-charter.md)
- [Hosting costs and subscription capacity](docs/architecture/hosting-and-capacity-plan.md)
- [Development standards and formatting](docs/development.md)
- [October 1 code review and remaining findings](docs/code-review-2026-10-01.md)

- [Chat](docs/runbooks/assistant.md), [conversations](docs/runbooks/conversations.md),
  [memory](docs/runbooks/shared-context.md), and [voice](docs/runbooks/remote-voice.md)
- [Google connections](docs/runbooks/google.md), [local files](docs/runbooks/local-files.md),
  [accounts](docs/runbooks/identity.md), and [persistence](docs/runbooks/persistence.md)
- [Creative applications and desktop control](docs/runbooks/agent-environments.md#creative-applications-and-desktop-control)
- [Agent profiles](docs/runbooks/agent-profiles.md), [execution](docs/runbooks/agent-execution.md),
  and the [company platform plan](docs/architecture/multi-agent-company-platform-plan.md)

Home control is independently deployed in [RobbinsHome](https://github.com/cam40419/RobbinsHome).
Simon connects through the optional `SIMON_HOME_API_URL` and `SIMON_HOME_API_TOKEN` HTTP
integration; home credentials and device drivers stay in that repository. See
[repository separation](docs/architecture/repository-separation.md) for cutover and recovery.

## Verify

```powershell
.\venv\Scripts\python.exe -m ruff check src tests scripts examples
.\venv\Scripts\python.exe -m ruff format --check src tests scripts examples
.\venv\Scripts\python.exe -m mypy src
.\venv\Scripts\python.exe -m pytest -q -m "not postgres and not browser and not live"
```

PostgreSQL tests require `SIMON_TEST_DATABASE_URL` pointing to a disposable database
ending in `_test`. Browser tests use `SIMON_BROWSER_TESTS=1` and Playwright Chromium
or `SIMON_BROWSER_CHANNEL=msedge`. Live model checks require explicit opt-in and use
API billing; the normal project-board tests use a synthetic provider.
