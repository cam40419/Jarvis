# Persistence and recovery

Use PostgreSQL for persistent development and hosted operation. The in-memory
adapter is for isolated tests and disposable sessions. See [local operation](local-operations.md)
for startup and [database testing](database-testing.md) for the isolated PostgreSQL runner.
`/health/live` reports process liveness; it does not prove database readiness.

## Transactions and retries

State mutations, successful idempotency receipts, audit records and outbox intents
share a transaction. Workspace writes are serialized to preserve audit ordering.
Retries reuse a scoped key and reject changed command content. Native project and
team services recheck current authorization before consulting a receipt.

The PostgreSQL adapter supports nested savepoints and a bounded process-local
connection pool. Tests cover rollback, concurrent claims, duplicate commands,
tenant isolation, audit chains and durable retries through the real adapter.
Configure only an isolated database whose name ends in `_test` for tests; a
configured but unreachable database fails instead of silently skipping checks.

External calls happen outside database transactions. A persisted claim can prevent
duplicate dispatch, but the database cannot roll back provider I/O. Unknown outcomes
require reconciliation. Outbox publication is at least once: consumers must
deduplicate by event ID if delivery succeeds before the transaction commits.

## Migrations

`python -m simon.migrate` serializes migrations with a database advisory lock. SQL
changes and checksum records commit together. Reruns skip unchanged versions,
reject changed or unknown versions, and roll back failures. Applied SQL files are
immutable ledger entries; add a numbered forward migration for schema changes.
Historical migration bytes remain for checksum verification and fresh database
construction, not as an application compatibility surface. Migration 0031 removes
the retired project file tables while adding native team persistence.

The runner refuses untracked pre-existing schemas. It does not silently adopt or
reset arbitrary databases. Down migrations are not executed automatically.

## Chat and native work

Private background chat requests are `assistant.session` jobs. The optional
assistant worker processes two sessions concurrently. Browser navigation does not
cancel a saved request. Interrupted execution is inspected before retrying so that
potential external effects are not blindly repeated. The machine and worker must
remain running to make progress.

Native projects, members, board tasks, agent roles, policies and scoped credential
metadata have explicit database records. [Native execution](native-execution.md) now
persists bounded workflows, grants, runner leases, checkpoints, waits and model liabilities.
Creating a role alone does not execute tasks; qualified models, enabled policy, explicit
admission and an authorized runner are required. [Artifacts and delivery](native-artifacts.md)
retain exact revisions, independent reviews, owner decisions and immutable delivery snapshots.

Migration 0040 adds immutable delivery profile assignments, validator evidence, package
acceptances and authorized continuity manifests. These records retain canonical identities,
scoped references and atomic storage charges. See the
[delivery/review/continuity runbook](delivery-review-continuity.md) for supported operations.

[Workspace storage](workspace-storage.md) adds immutable entitlement history and atomic
retained-content charges in migration 0037. A source upload commits its reservation before
filesystem publication. Database rollback leaves that reservation held until finalization
or owner reconciliation proves safe cleanup. Backfill accounts existing source metadata,
artifact revisions and packet manifests/previews; it does not discover legacy orphan files.

## Backup

Use [storage recovery](storage-recovery.md) for full bundles containing the database,
files and matching encryption keys. Restore into a separate empty database and
verify before switching an installation. Off-machine retention and disaster
recovery targets are deployment responsibilities. Never treat a successful source
test run as proof that a particular installed database has been migrated or backed up.
