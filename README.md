# Simon

**SIMON: Somehow It Manages Our Nonsense.**

Simon is a local-first foundation for a hosted AI work platform. Its browser workspace
currently provides shared projects, task boards, human membership and scoped agent teams,
alongside independent conversations. PostgreSQL persists project and identity records.

In **Projects**, describe an outcome, create tasks, and assign work to a person, an agent
or the shared pool. **Agents** defines each role's responsibilities, success criteria and
reason for joining the team. Owners control team size, delegated staffing and temporary
worker access. Pausing or retiring an agent returns unfinished assignments to the pool.

**Intake** accepts versioned source files, uses a configured model to propose the next
milestone and team, and makes a separate model review before applying roles and board work.
Each project controls cloud consent, automatic staffing and a bounded planning allowance.
Native task execution, artifact reviews and full project provider/key/budget management
remain subsequent core work. Creating a role does not start a worker.
The old Work interface, manifest dispatcher and separate project/task authorities have
been removed. There is no legacy-project migration or compatibility mode.

## Run locally

Use Python 3.11+ and a local virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -e '.[dev,postgres]'
```

Configure `.env` from [.env.example](.env.example). For a configured local installation,
start the Compose database with `.\scripts\start-local.ps1 -DatabaseOnly` when using that local database, then start the configured server with `.\scripts\start-local.ps1` and open
**http://localhost:8000/login**. Independent queued conversations use
`.\scripts\start-assistant-worker.ps1`; this does not execute native board tasks.
New installations can use `.\scripts\start-dev.ps1 -Memory -TestRunner` for a
nonpersistent smoke run. Follow [identity setup](docs/runbooks/identity.md) and
[local operations](docs/runbooks/local-operations.md) for persistent accounts.

## Guides

- [Projects, scoped agents and team management](docs/runbooks/native-projects.md)
- [AI-assisted intake, model configuration and automatic staffing](docs/runbooks/project-intake.md)
- [Roadmap and delivery order](docs/next-phases.md)
- [Platform architecture and implementation backlog](docs/architecture/autonomous-work-platform-plan.md)
- [Clothing-brand pilot and core acceptance](docs/architecture/clothing-brand-pilot-charter.md)
- [Integration work packages](docs/architecture/integration-delivery-plan.md)
- [Hosting and subscription capacity](docs/architecture/hosting-and-capacity-plan.md)
- [Development standards](docs/development.md)
- [Isolated database testing](docs/runbooks/database-testing.md)
- [Storage recovery](docs/runbooks/storage-recovery.md) and [remote access](docs/runbooks/remote-access.md)

The platform is the deliverable; stdout is its pilot. Complete and test the core before
accepting individual business tools. Existing generic adapters are reusable implementation
components, not accepted native-project integrations.

## Verify

```powershell
.\venv\Scripts\python.exe -m ruff check src tests scripts examples
.\venv\Scripts\python.exe -m ruff format --check src tests scripts examples
.\venv\Scripts\python.exe -m mypy src
.\venv\Scripts\python.exe -m pytest -q -m "not postgres and not browser and not live"
npm run format:check
npm run lint
```

PostgreSQL tests use an isolated database ending in `_test`; the database runbook describes
the disposable-cluster runner. Browser tests use `SIMON_BROWSER_TESTS=1` and installed
Playwright Chromium or `SIMON_BROWSER_CHANNEL=msedge`. Normal acceptance uses synthetic
providers. Paid model tests require separate explicit opt-in.
