# Isolated PostgreSQL acceptance

Use a disposable database to verify migrations, persistence, authorization and concurrent commands. The deployed database and application `.env` are not test fixtures. The [native project contract](native-projects.md) describes the first replacement schema; [development standards](../development.md) describe the other checks.

## Run against a new local cluster

`scripts/test_postgres.py` accepts a PostgreSQL binary directory containing `initdb` and `pg_ctl`. That installation must include `pgcrypto` and `pgvector` built for its PostgreSQL major version. The runner does not install packages or register a service.

```powershell
.\venv\Scripts\python.exe scripts\test_postgres.py --bin-dir .local\db-phase\pg16\Library\bin
```

Each invocation creates a separate directory under `.local/postgres-tests`, initializes a cluster with a generated SCRAM password, listens on an available loopback port, and creates `simon_acceptance_test`. It supplies the connection settings only to its pytest child. Inherited PostgreSQL connection variables are removed. No shell activation or global environment changes are required.

The default selection is `-q -m "postgres and not browser and not live"`, including the workspace identity and native project migration tests. Browser and paid-model checks remain separate opt-in workflows. To select the new project acceptance cases:

```powershell
.\venv\Scripts\python.exe scripts\test_postgres.py --bin-dir .local\db-phase\pg16\Library\bin -- -q -m postgres tests/contract/test_native_project_store.py tests/api/test_native_project_postgres_api.py tests/integration/test_native_project_migration.py tests/integration/test_workspace_migration.py
```

Arguments after `--` are passed to pytest. Set `--test-timeout` before `--` to override the default 1,800-second limit. The runner returns pytest's failure status. On timeout or interruption it terminates its own pytest process tree, then allows up to 90 seconds for database shutdown. Setup failures also trigger database cleanup. If cleanup fails, the command fails and identifies its own cluster directory for investigation. It never adopts or stops an existing cluster.

The printed directory retains `initdb.log`, `server.log`, `lifecycle.log`, `pytest.log` and a password-free `result.json` with the server/extension versions and exit status. The temporary password file is removed after initialization. These ignored directories contain synthetic test data; inspect shutdown status before removing them. They are diagnostics, not application storage or backups.

## Binary sources and version boundaries

The repository's CI/container target is PostgreSQL 17 with pgvector. Docker Desktop could not start on the current Windows development host, so a portable Windows installation was prepared under `.local/db-phase` using PostgreSQL 16.15 and pgvector 0.8.6 from conda-forge. This does not change the hosted target. Local results must identify their actual database version; a PostgreSQL 16 pass does not establish a PostgreSQL 17 run.

[PostgreSQL lists Windows binary archives](https://www.postgresql.org/download/windows/). The [pgvector installation guide](https://github.com/pgvector/pgvector#installation) documents Docker, source builds and conda-forge. [Micromamba's manual installation instructions](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html#manual-installation) provide a portable package manager. Keep its root/cache and PostgreSQL prefix under the ignored workspace directory; omit shell initialization and host service registration. The application does not depend on Micromamba.

For the prepared Windows package pair, an explicit install uses:

```powershell
.\.local\db-phase\micromamba.exe create --no-rc --no-env -r .local/db-phase/mamba -p .local/db-phase/pg16 --override-channels -c conda-forge --no-shortcuts --skip-run-link-scripts -y postgresql=16.15 pgvector=0.8.6
```

Use matching server/extension packages. Do not copy a PostgreSQL 16 extension DLL into a PostgreSQL 17 installation. If a corporate certificate policy prevents a package download, resolve that prerequisite separately; the test runner itself requires no network downloads.

## Existing disposable database or CI

An explicitly configured `SIMON_TEST_DATABASE_URL` can instead point to a separate disposable PostgreSQL server. Its database name must end in `_test`. The shared pytest fixture creates the required extensions, assigns each test a fresh schema and drops only that schema afterward. Its function-scoped connection pool closes before schema cleanup; restart tests still reconstruct independent adapters and applications. Run `python -m pytest -q -m "postgres and not browser and not live"` with that variable set. Never point it at an application database or use deployed Compose volumes for these tests.

Database acceptance covers fresh migration/replay, upgrade preservation, failed-DDL rollback/retry, tenant foreign keys, durable receipts and sessions, independent connection races, and revocation. Production backup/restore procedures, hosted capacity measurements and release deployment remain separate work.

## October 7 local acceptance

The PostgreSQL 16.15 / pgvector 0.8.6 sweep selected `-m postgres`: 380 tests passed, 15 optional browser/live cases skipped, and one case encountered a database connection timeout during setup. The setup stalled for approximately 794 seconds during a long host pause also visible in checkpoint logs. The test body had not run. A fresh-cluster rerun of `test_concurrent_publish_keys_cannot_create_a_second_mapping[postgres]` passed in 3.08 seconds. All 381 nonoptional database cases therefore passed across the sweep and targeted rerun; this was not one uninterrupted green run.

The 22 native-project database cases are included in that coverage. Runner lifecycle, timestamp and native service/API checks also passed: 53 tests, with one Windows symlink-permission skip. Lint, formatting and strict application type checks passed. The database API checks explicitly set a non-UTC session timezone and retain exact response comparisons.

All temporary clusters were stopped. The broad run needed approximately 39 seconds to finish its shutdown checkpoint, exceeding the runner's original 30-second deadline. Its original failure receipt remains intact; a subsequent `pg_ctl status` independently confirmed no server running. The runner now allows 90 seconds. PostgreSQL durability settings were not weakened to accelerate these checks.

PostgreSQL 17, browser/live checks and deployment acceptance remain unverified by this local run. CI retains its existing PostgreSQL 17 service and complete release checks.
