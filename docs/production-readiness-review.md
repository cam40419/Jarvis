# Production readiness review

Reviewed October 5, 2026. Target: one administrator and invited users, hosted models,
UI-managed account connections, and an independent RobbinsHome service.

This is a repository review, not a penetration test or a concurrency benchmark.
Findings below distinguish confirmed behavior from changes that still need design or testing.
Existing unrelated work in the working tree was preserved. No historical data was deleted.

## Code overview

- `domain/` contains immutable validated data models and the storage/tool contracts.
- `services/` contains account authorization, conversations, jobs, project coordination,
  agent execution, evidence validation, artifacts and connected-action workflows.
- `adapters/` implements PostgreSQL and in-memory storage, external providers, model access,
  files, tools, containers and dedicated-machine runners.
- `api/` exposes authenticated FastAPI routes and serves the HTML/CSS/JavaScript UI.
- Separate assistant and agent processes claim durable work; agent environment lease state
  also lives in SQLite under the agent state directory.
- `db/migrations/` provides checksum-verified schema upgrades; `scripts/` currently supplies
  Windows/local deployment, backup and maintenance operations.

The intended separation is sound. Production work should preserve domain/service boundaries,
move process setup out of import-time execution, and make operational state explicit. The
largest complexity is project orchestration and provider reconciliation rather than HTTP routing.

## Identity changes completed

- Application models, services, storage queries, identity commands, authentication responses,
  frontend workspace selection, examples, and tests use `workspace_id` and `workspace_name`.
- `actor_id` remains the internal user identifier. It is not a home/device identity and does
  not need to be renamed to provide real accounts. Connection ownership is derived from the session.
- Migration `0028_workspace_identity.sql` renames the `households` table and tenant columns,
  and changes shared thread/memory scope values to `workspace`. It preserves UUIDs, foreign
  keys, memberships, sessions, encrypted connections, and historical audit contents.
- Historical SQL migrations remain unchanged because the migration runner verifies their checksums.
- Historical model snapshots and environment settings remain readable. New model serialization
  uses workspace names. Ambiguous identity inputs are rejected by strict model validation.
- ClickUp grants use `workspace_id` for Simon and `clickup_workspace_id` for ClickUp. Older
  grants containing both `household_id` and ClickUp's `workspace_id` are translated on input.
- Production configuration rejects the development administrator/workspace placeholder UUIDs.
- RobbinsHome still receives its version-one `X-Household-ID` header and household receipt
  fields through the adapter. Simon's workspace ID supplies that explicit external mapping;
  this does not combine the services' databases or authentication.
- The audit hash input keeps its historical household key. Changing a hash input would
  invalidate existing verification. Public audit model fields use workspace naming.
- External-provider fingerprints also preserve their historical hash format so saved action
  review receipts remain valid when the tenant field is renamed.

Upgrade test: create a database with migrations 0001–0027, insert historical memberships,
sessions, threads, connection snapshots and audit records, apply 0028, and verify data reads,
unchanged snapshots/hashes, and repeat migration behavior.

## Launch blockers and security

| Priority | Confirmed behavior / evidence                                                                                                                                  | Required production work                                                                                                                                                                                                                                      |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| High     | `api/auth.py` now supports password authentication on HTTPS domains; passkey UI controls were removed.                                                         | Validate password sign-in and invitation acceptance on the final HTTPS domain.                                                                                                                                                                                |
| High     | `services/accounts.py` still exposes invitations for manual delivery; verified email recovery is now implemented.                                              | Configure encrypted email delivery through Connections, verify existing recovery emails, and add invitation email delivery. Keep registration invitation-only.                                                                                                |
| High     | `config.py` defaults to effectively unlimited session lifetime.                                                                                                | Select a production expiry policy; add active-session visibility and revocation. Preserve existing local behavior until the UI supports it.                                                                                                                   |
| High     | Connection encryption can use a generated local file (`services/integrations.py`). Multiple hosts with independent files cannot decrypt one another's records. | Provision one stable protected key shared by the API/workers, back it up separately, and document rotation. Production startup must verify availability before accepting connections.                                                                         |
| High     | API and workers need local files and agent state; deployment compose currently primarily supplies development PostgreSQL.                                      | Supply a production application image, Linux startup, shared durable storage, migration procedure, and health checks for all processes.                                                                                                                       |
| High     | Docker execution is controlled by a host process in `adapters/execution_backends.py`.                                                                          | Keep Docker control away from the public API container and isolate agent execution from application credentials/database storage. Test resource exhaustion, process termination, network access, and runner ownership.                                        |
| Medium   | `api/request_ingress.py` authentication buckets are process-local.                                                                                             | Establish a trusted proxy configuration and shared or edge-enforced rate limits before scaling replicas. Test both client-IP spoofing and users sharing an IP.                                                                                                |
| Medium   | UI-managed home/gateway endpoints accept user-supplied URLs. HTTPS validation alone does not establish a safe destination.                                     | Define outbound destination policy and test DNS/private-address/redirect behavior. RobbinsHome may legitimately require private-network access; separate trusted connector access from general outbound requests. This review did not demonstrate an exploit. |
| Medium   | Hosted model access is configured at installation level; user invitation count is capped at 25, but that is not an AI-spend budget.                            | Add per-user usage/concurrency limits, installation spending controls, and an administrator usage view.                                                                                                                                                       |
| Medium   | Backup scripts are tied to the local Compose database and paths; private bundles can include credentials and are unencrypted.                                  | Add encrypted off-server backups for the hosted layout, retention, access restrictions, restore drills, and restore of encryption keys as well as database/files.                                                                                             |
| Medium   | Python dependencies use broad version ranges; no Python lockfile is present.                                                                                   | Build from a reproducible lock, scan dependencies and images, and record the tested versions in release artifacts.                                                                                                                                            |

Existing strengths: strict model inputs, Argon2 password hashes, passkey support, secure
HTTP-only cookies on HTTPS, origin/CSRF checks, trusted-host checks, security headers,
bounded request bodies, account/workspace authorization, encrypted connection records,
and explicit action confirmation. Google/external actions commit execution claims before
network I/O and avoid automatically replaying uncertain writes. Preserve these behaviors
during cleanup rather than replacing them with a simpler but weaker flow.

## Efficiency and scaling

1. **Audit append is proportional to accumulated history.** `services/audit.py` calls
   `audit_events(workspace_id)` to obtain its length and final hash for every event. Fetch
   only the latest sequence/hash under the existing workspace transaction lock, or keep a
   transactional audit-head row. Verify concurrent append and historical chain behavior.
2. **Repeated Google application-settings reads.** `ConnectedService.settings` can resolve
   saved administrator membership/connections repeatedly, including tool readiness checks.
   Resolve settings once per request/operation or use a bounded version-aware cache. Keep
   disconnect/credential rotation visible across processes.
3. **Account administration performs repeated queries.** Account descriptions separately
   load enrollment, password and passkeys. Batch these reads if account counts grow. With
   the current 25-account cap this is lower priority than the audit path.
4. **Database connections need a deployment-wide budget.** The API uses a pool of up to 16.
   Multiply that by API replicas and include worker connections before selecting database
   capacity. Make pool sizing configurable and add pool-wait metrics.
5. **Large artifact bytes are stored in PostgreSQL.** `adapters/postgres.py` persists project
   artifact content in the database. Keep small metadata/transactional references there;
   consider object storage for large retained artifacts and uploads. Measure actual growth
   before migrating, and preserve integrity hashes and authorization.
6. **Shared local paths limit independent scaling.** Move file access behind a storage
   interface before distributing workers across hosts. Retain local storage for development
   and choose object storage/shared filesystem according to actual execution requirements.
   Environment lease coordination uses `environment-leases.sqlite3`; multiple hosts also
   need a supported central lease coordinator rather than independent SQLite files or an
   assumed network-filesystem SQLite deployment.
7. **Measure throughput before advertising capacity.** Add timings for request handlers,
   provider calls, job queue age, execution duration, pool waits, and token usage. Hosted-model
   waiting, browser execution, and document conversion have different resource profiles.

## Cleanliness and simpler refactors

- **Separate application construction from module import.** `api/app.py` creates an application
  at import time. With persisted integration settings this can access PostgreSQL before test
  fixtures or migration preparation run. Use an ASGI factory or a minimal explicit entrypoint;
  test imports without network/database access.
- **Split large orchestration modules around responsibilities.** In this checkout,
  `project_coordinator.py` is about 1,660 lines, `project_boards.py` about 1,480,
  `agent_worker.py` about 1,380, and `project_work.py` about 1,210. Good seams include planning,
  run recovery, provider reconciliation, evidence validation and output publication. Extract
  one seam at a time with behavioral tests; a filename change alone provides little benefit.
- **Give HTTP routers their own modules.** `api/app.py` mixes service construction, security
  middleware, static rendering and many routes. Keep dependency wiring and lifecycle there;
  extract conversation/preferences/connection routes using the existing router pattern.
- **Centralize administrator authorization.** `AccountService.is_admin` requires the
  configured administrator identity and current workspace ownership, while Google app setup
  in the integration API checks the configured identity alone. Use one policy in the UI
  metadata and mutation handlers, including role-change/revocation regression tests.
- **Centralize connection lookup and request-scoped settings.** Several service factories
  construct integration resolvers. Share a clear ownership/lifetime model while preserving
  live credential refresh and account boundaries.
- **Document state transitions once.** Google actions, external actions and board actions
  have related claim/revalidate/execute/reconcile patterns. Extract only truly common
  transitions; provider-specific uncertainty must remain explicit.
- **Replace broad compatibility with a scheduled retirement plan.** Legacy `JARVIS_` settings,
  workspace input aliases and old connection grants help upgrades today. Record which are
  still used, migrate them, then remove them in a declared breaking release.
- **Rename misleading operational labels separately.** Distribution name `simon-homeos` and
  the local assistant task `Simon-Workflow` retain older terminology. Renaming packages or
  scheduled tasks requires an installation upgrade, not blind deletion.

`PostgresStore` inherits `InMemoryStore`, but inspection found only capability registry
`get`, `list`, and `register` are inherited. No missing persistence override was identified
by that comparison. Composition could clarify this boundary; it is not a confirmed data-loss bug.

## Removal candidates

| Candidate                                             | Evidence and disposition                                                                                                                                                                                                                                                                                                                                                                               |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Retired physical-home database tables                 | `home_devices`, `home_syncs`, `home_commands`, and `power_samples` remain in historical migrations, but current Simon storage code has no table operations for them. The local database still contains 9 devices, 3 syncs, 404 command records and 76,699 power samples. Archive/export and confirm RobbinsHome retention first, then remove through a new migration. Do not delete migration history. |
| Retired workflow database tables                      | Current Simon code has no operations against `workflow_versions`, `workflow_runs`, or `workflow_events`; local historical counts are 5, 18 and 10,781. Archive these before a removal migration, and inspect all workflow tables including workers. The current `workflow_worker.py` runs assistant work and is not itself obsolete.                                                                   |
| File-based integration onboarding                     | Legacy external-provider/project-board loaders and examples duplicate UI-managed connections. Remove user-facing file setup after confirming all needed providers/settings have UI equivalents and migrating remaining installations. Keep administrator-managed execution/tool policy separate from account linking.                                                                                  |
| Development identity in production distribution paths | Keep deterministic fixtures for tests/local development; restrict production bootstrap to real identities. The new production validation enforces this. Do not remove constants while tests depend on them.                                                                                                                                                                                            |
| Legacy configuration aliases                          | Remove after installations have upgraded and historical snapshots have a supported migration path. Removing aliases now would break existing saved records.                                                                                                                                                                                                                                            |
| One-off migration/backfill/cleanup scripts            | Consolidate completed operations into an operator-only maintenance area with usage dates and prerequisites. They are candidates for later removal, not proven dead code; some are needed for recovery.                                                                                                                                                                                                 |

The legacy `start-server.ps1` wrapper duplicates configured startup and embeds domain,
database and identity defaults. Consolidate on the configured launcher after updating
installation paths and preserving upgrade compatibility.

No optional browser, desktop, CAD, Adobe, or connector capability was classified as dead
merely because it is not needed by the first hosted deployment. Exclude these dependencies
and images from the default hosted profile; remove an entire feature only after a product decision.

## Deployment sequence

1. Back up and verify restoration. Drain API/workers together before migration 0028; old
   processes refer to the old table/columns. Do not mix old/new application versions.
2. Apply the migration and start all processes from the new code. Refresh clients for
   `/auth/workspace` and workspace response fields. Existing server sessions retain their IDs.
3. Finish invitation-based hosted account flows and the administrator UI setup.
4. Package API/assistant/dispatcher for Linux; define stable shared credentials/storage,
   isolated runners, PostgreSQL with pgvector, email, monitoring and encrypted backups.
5. Run domain-specific HTTPS/auth tests, cross-user/workspace isolation tests, restore drills,
   runner isolation tests and a small concurrency load test before opening invitations.

The identity migration prepares the account model; it does not by itself make the
application production-ready or deploy it to a hosting provider.

## Validation and local installation

- Created `.local/backups/workspace-identity-before-0028`, including private deployment
  settings and the connection key, and restored its database into a disposable database.
  Verification matched all 43 tables and checked the 507-file recovery bundle.
- Drained the three scheduled services, applied migration 0028 to the local database, and
  verified unchanged workspace, membership, authentication session, connection and audit
  row counts. Updated the local account setting name automatically.
- Restarted API, assistant and agent services. Health, authentication configuration,
  login HTML and updated login JavaScript return HTTP 200; unauthenticated integration
  requests return HTTP 401. The administrator membership resolves and the saved integration
  credential still decrypts. The installation remains in its previous environment mode.
- Browser identity and integration connection lifecycle tests passed against PostgreSQL.
- The broad regression sweep also found planning recovery tests expecting `blocked` where
  the implementation returns `waiting`. These require reconciliation before deployment;
  the identity migration does not change that planner decision logic. Do not advertise a
  fully passing repository suite until all remaining failures have been resolved.

- The broad sweep completed with 2,632 passing cases and two POSIX-only skips. It also
  exposed stale dispatcher/HTTPS/production-ID fixtures; these were corrected and their
  60-case regression group passed on rerun. The separate focused identity/API group passed
  116 cases, and both browser cases passed. Ruff, mypy and ESLint passed.
- **Ten reproduced regression failures remain:** eight cases in
  `tests/unit/test_project_planning_recovery.py` (`waiting` versus `blocked`), and both
  storage backends for `test_review_acceptance_roundtrip_and_replacement` in
  `tests/contract/test_agent_run_store.py` (the expected completed-task artifact tuple is
  empty). Investigate whether the artifact fixture or publication behavior is wrong;
  this review does not establish its root cause. These are explicit production blockers.
  The broad suite was not rerun after fixture corrections; targeted reruns verified the
  corrected fixtures and reproduced these remaining failures.

## Agent and connection redesign

The current follow-up plan is [agent-project-connections-plan.md](agent-project-connections-plan.md).
Roles now share authorized workspace tools; unrelated configuration changes no longer invalidate
saved assignments. Connections provides the full current catalog, UI account setup and explicit
read-only tests. Production work remains for generic extension installation, storage OAuth refresh,
administrator provisioning, runtime health and the hosting controls listed in this review.
