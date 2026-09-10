# Jarvis

Jarvis is a personal assistant control plane paired with the separately deployable Home OS
physical-automation subsystem. The repository is being rebuilt from first principles; the legacy
runtime remains recoverable from Git history but is no longer part of the working tree.

## Current foundation

- Strict, immutable domain contracts
- Deny-by-default capability authorization
- Atomic idempotency in the reference adapter
- Durable-job transitions with optimistic version checks
- A tamper-evident audit chain
- A minimal FastAPI vertical slice with stable errors
- A PostgreSQL/pgvector schema and local Compose service
- Linting, strict typing, coverage, and CI gates

No live model, account, credential, or physical-device integration is enabled. Hard and dangerous
writes fail closed until signed confirmation verification exists.

## Development

```powershell
py -3.11 -m venv venv
.\venv\Scripts\python.exe -m pip install -e ".[dev]"
.\venv\Scripts\python.exe -m ruff check src tests
.\venv\Scripts\python.exe -m mypy src
.\venv\Scripts\python.exe -m pytest --cov=jarvis --cov-report=term-missing
```

Run the in-memory API:

```powershell
.\venv\Scripts\python.exe -m uvicorn jarvis.api.app:app --reload
```

The temporary header identity adapter requires `X-Actor-Id`, `X-Household-Id`, and optionally
`X-Scopes`. It is a development seam, not deployment-ready authentication.

Start the development database with:

```powershell
docker compose -f deploy/compose/compose.yaml up -d
```

See the [target architecture](docs/architecture/target-architecture.md) and architecture decisions
under [`docs/decisions`](docs/decisions). The ordered prerequisites for the next build are in the
[Phase 1 readiness checklist](docs/phase-1-readiness.md).
